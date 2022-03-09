import joblib
import numpy as np
import pandas as pd
import scanpy as sc
from pathlib import Path
from sklearn.linear_model import LogisticRegression
from scipy.sparse import save_npz, load_npz
from pyscenic.aucell import aucell, derive_auc_threshold
from pyscenic.genesig import GeneSignature


def init_core_model(core_model_str):
    if core_model_str == "LogisticRegression":
        return LogisticRegression()
    else:
        raise NotImplementedError(
            f"core model '{core_model_str}' is not yet implemented. "
            f"Please try 'LogisticRegression' instead."
        )


def score_cells(adata, name, signatures_gmt, out_dir, scorer):
    if scorer == "AUCell":
        gs = GeneSignature.from_gmt(
            str(signatures_gmt), field_separator="\t", gene_separator="\t"
        )
        df = adata.to_df()
        percentiles = derive_auc_threshold(df)
        scores = aucell(
            exp_mtx=df,
            signatures=gs,
            auc_threshold=percentiles[0.01],
            seed=2,
            normalize=True,
        )
    else:
        raise NotImplementedError(
            f"scorer '{scorer}' is not yet implemented." f"Please try 'AUCell' instead."
        )
    path = Path.cwd() / out_dir / name
    path.mkdir(parents=True, exist_ok=True)
    scores.to_csv(path / "scores.csv", index=False)


def calculate_connectivities(
    adata, name, signatures_gmt, n_neighbors, use_highly_variable, out_dir
):
    # just consider genes from the gene lists
    sig = pd.read_csv(signatures_gmt, header=None, sep="\t")
    genes = sig.iloc[:, 2:].values.flatten()
    genes = np.unique(genes[~pd.isnull(genes)]).tolist()
    genes_in_var = list(set(genes) & set(adata.var["gene_symbol"].values.tolist()))
    adata = adata[:, genes_in_var]

    sc.pp.highly_variable_genes(adata)
    sc.tl.pca(adata, use_highly_variable=use_highly_variable)
    sc.pp.neighbors(adata, n_neighbors=n_neighbors, method="umap")
    sparse = adata.obsp["connectivities"]
    path = Path.cwd() / out_dir / name
    path.mkdir(parents=True, exist_ok=True)
    save_npz(path / "connectivities_sparse.npz", sparse)
    return sparse.todense()


def propagate_labels(
    core_pred_proba, scores, connectivities, n_iter, certainty_threshold
):
    certainty_info = scores.copy()
    absdif = abs(scores.max(axis=1) - scores.min(axis=1))
    final_pred_proba = core_pred_proba
    for i in range(n_iter):
        certainty_threshold_pct = certainty_threshold * np.exp(-0.3 * i)
        certainty_info[f"certain{i}"] = False
        certainty_info.loc[
            absdif > absdif.quantile(q=certainty_threshold_pct), f"certain{i}"
        ] = True
        final_pred_proba.loc[certainty_info[f"certain{i}"] == False] = 0.000001

        final_step_mtx = np.dot(connectivities, final_pred_proba.values)
        final_step_mtx = np.divide(final_step_mtx, final_step_mtx.sum(axis=1))
        final_pred_proba.loc[:, :] = final_step_mtx

        current = final_pred_proba.idxmax(axis=1)
        if not i < 1:
            if ((current != pre).sum() / current.size) < 0.001:
                print(
                    f"converged at iteration step: {i+1} with "
                    f"{((current != pre).sum() / current.size):.4f} < 0.001"
                )
                break
        if i == n_iter - 1:
            print(
                f"Warning: Label propagation did not converge "
                f"({((current != pre).sum() / current.size):.4f} >= 0.001) "
                f"within {n_iter} iterations!"
            )
        pre = current

    return final_pred_proba.idxmax(axis=1), final_pred_proba


def check_signatures_overlap(
    signatures_gmt, adata, name, out_dir, adapt_signatures
):
    if not adapt_signatures:
        return signatures_gmt
    
    sig = pd.read_csv(signatures_gmt, header=None, sep="\t")
    tumor_genes = sig.loc[(sig == "Tumor").any(axis=1), 2:].dropna(axis=1).values.flatten().tolist()
    tumor_genes_in_var = list(set(tumor_genes) & set(adata.var["gene_symbol"].values.tolist()))
    normal_genes = sig.loc[(sig == "Normal").any(axis=1), 2:].dropna(axis=1).values.flatten().tolist()
    normal_genes_in_var = list(set(normal_genes) & set(adata.var["gene_symbol"].values.tolist()))
    if (len(tumor_genes_in_var) / len(tumor_genes) < 0.8) or (len(normal_genes_in_var) / len(normal_genes) < 0.8):
        gmt = pd.DataFrame([normal_genes_in_var, tumor_genes_in_var], index=["Normal", "Tumor"])
        gmt.insert(0, "00", "ikarus")
        path = Path.cwd() / out_dir / name
        path.mkdir(parents=True, exist_ok=True)
        gmt.to_csv(path / "signatures_tmp.gmt", header=None, sep="\t")
        print(
            f"Less than 80% of signature genes are available in data set. "
            f"A temporary signature is stored where non-overlapping genes are removed. "
            f"It is proceeded with the temporary signature."
        )
        return path / "signatures_tmp.gmt"
    else:
        return signatures_gmt
    

class Ikarus:
    def __init__(
        self,
        signatures_gmt,
        out_dir,
        scorer="AUCell",
        core_model="LogisticRegression",
        n_neighbors=100,
        use_highly_variable=False,
        adapt_signatures=False,
        n_iter=50,
        certainty_threshold=0.9,
    ):
        self.results = pd.DataFrame()
        self.signatures_gmt = signatures_gmt
        self.scorer = scorer
        self.out_dir = out_dir
        self.core_model = init_core_model(core_model)
        self.n_neighbors = n_neighbors
        self.use_highly_variable = use_highly_variable
        self.adapt_signatures = adapt_signatures
        self.n_iter = n_iter
        self.certainty_threshold = certainty_threshold
        self.fitted = False
        self.predicted = False

    def fit(
        self,
        adatas_list,
        names_list,
        obs_columns_list,
        scores_path_list=None,
        save=False,
    ):
        if not scores_path_list:
            scores_path_list = []
            for adata, name in zip(adatas_list, names_list):
                signatures_gmt = check_signatures_overlap(
                    self.signatures_gmt, adata, name, self.out_dir, self.adapt_signatures
                )
                score_cells(adata, name, signatures_gmt, self.out_dir, self.scorer)
                # Future: return scores directly from function score_cells instead
                # of building path here. For this it needs to be checked if read
                # scores and returned scores are of the same structure.
                path = Path.cwd() / self.out_dir / name
                path.mkdir(parents=True, exist_ok=True)
                scores_path_list.append(path / "scores.csv")

        scores_list = []
        labels_list = []
        for adata, obs_column, scores_path in zip(
            adatas_list, obs_columns_list, scores_path_list
        ):
            scores = pd.read_csv(scores_path, index_col=False)
            labels = adata.obs[obs_column]
            if scores.shape[0] != labels.shape[0]:
                raise IndexError(
                    f"Number of cells ({labels.shape[0]}) does not match number of "
                    f"scores ({scores.shape[0]}). If scores paths were provided "
                    f"please check if scores correspond to adata."
                )
            scores_list.append(scores)
            labels_list.append(labels)

        X_train = pd.concat(scores_list, axis=0, ignore_index=True)
        y_train = pd.concat(labels_list, axis=0, ignore_index=True)

        _ = self.core_model.fit(X_train, y_train)
        self.fitted = True

        if save:
            path = Path.cwd() / self.out_dir
            path.mkdir(parents=True, exist_ok=True)
            joblib.dump(self.core_model, path / "core_model.joblib")

    def predict(
        self, adata, name, scores_path=None, connectivities_path=None, save=False
    ):
        if not self.fitted:
            raise RuntimeError("Model not yet fitted. Please run Model.fit(...) first!")

        if not scores_path:
            signatures_gmt = check_signatures_overlap(
                self.signatures_gmt, adata, name, self.out_dir, self.adapt_signatures
            )
            score_cells(adata, name, signatures_gmt, self.out_dir, self.scorer)
            # Future: return scores directly from function score_cells instead
            # of building path here. For this it needs to be checked if read
            # scores and returned scores are of the same structure.
            path = Path.cwd() / self.out_dir / name
            path.mkdir(parents=True, exist_ok=True)
            scores_path = path / "scores.csv"

        scores = pd.read_csv(scores_path, index_col=False)
        y_pred = self.core_model.predict(scores)
        y_pred_proba = self.core_model.predict_proba(scores)

        self.results = scores.copy()
        self.results["core_pred"] = y_pred
        for i, scoring_label in enumerate(scores.columns):
            self.results[f"core_pred_proba_{scoring_label}"] = y_pred_proba[:, i]

        if not connectivities_path:
            signatures_gmt = check_signatures_overlap(
                self.signatures_gmt, adata, name, self.out_dir, self.adapt_signatures
            )
            connectivities = calculate_connectivities(
                adata,
                name,
                signatures_gmt,
                self.n_neighbors,
                self.use_highly_variable,
                self.out_dir,
            )
        else:
            connectivities = load_npz(connectivities_path).todense()
            if connectivities.shape[0] != adata.shape[0]:
                raise IndexError(
                    f"Shape of connectivities matrix ({connectivities.shape}) does "
                    f"not match number of cells ({adata.shape[0]}). Please check "
                    f"if provided connectivity matrix corresponds to adata."
                )

        final_pred, final_pred_proba = propagate_labels(
            pd.DataFrame(y_pred_proba, columns=scores.columns.tolist()),
            scores,
            connectivities,
            self.n_iter,
            self.certainty_threshold,
        )
        self.results["final_pred"] = final_pred
        for i, scoring_label in enumerate(scores.columns):
            self.results[f"final_pred_proba_{scoring_label}"] = final_pred_proba.iloc[
                :, i
            ]

        if save:
            path = Path.cwd() / self.out_dir / name
            path.mkdir(parents=True, exist_ok=True)
            self.results.to_csv(path / "prediction.csv")
        self.predicted = True
        return final_pred.values

    def get_umap(self, adata, name, random_state=0, save=False):
        if not self.predicted:
            _ = self.predict(
                adata, name, scores_path=None, connectivities_path=None, save=save
            )

        np.random.seed(random_state)
        adata.obs["core_pred"] = self.results["core_pred"].values
        adata.obs["final_pred"] = self.results["final_pred"].values

        sc.tl.pca(adata, random_state=random_state)
        sc.pp.neighbors(adata, n_neighbors=self.n_neighbors, method="umap")

        sc.tl.umap(adata, random_state=random_state)
        if save:
            path = Path.cwd() / self.out_dir / name
            path.mkdir(parents=True, exist_ok=True)
            adata.write_h5ad(path / "adata_umap.h5ad")
        return adata

    def load_core_model(self, core_model_path):
        self.core_model = joblib.load(core_model_path)
        self.fitted = True

    def cnv_correct(
        self, cnv_df, adata, name, connectivities_path=None, label_propagation=False, save=False
    ):
        from sklearn.linear_model import LogisticRegression
        X = cnv_df
        y = self.results["final_pred"].values
        model = LogisticRegression(max_iter=1000)
        model.fit(X, y)
        y_pred = model.predict(X)

        # optional: repeat label propagation
        if label_propagation:
            y_pred_proba = model.predict_proba(X)
            if not connectivities_path:
                signatures_gmt = check_signatures_overlap(
                    self.signatures_gmt, adata, name, self.out_dir, self.adapt_signatures
                )
                connectivities = calculate_connectivities(
                    adata,
                    name,
                    signatures_gmt,
                    self.n_neighbors,
                    self.use_highly_variable,
                    self.out_dir,
                )
            else:
                connectivities = load_npz(connectivities_path).todense()
                if connectivities.shape[0] != adata.shape[0]:
                    raise IndexError(
                        f"Shape of connectivities matrix ({connectivities.shape}) does "
                        f"not match number of cells ({adata.shape[0]}). Please check "
                        f"if provided connectivity matrix corresponds to adata."
                    )

            y_pred, _ = propagate_labels(
                pd.DataFrame(y_pred_proba, columns=model.classes_.tolist()),
                pd.DataFrame(y_pred_proba, columns=model.classes_.tolist()),
                connectivities,
                self.n_iter,
                self.certainty_threshold,
            )

        self.results["final_pred_cnv_corrected"] = y_pred
        if save and name:
            path = Path.cwd() / self.out_dir / name
            path.mkdir(parents=True, exist_ok=True)
            self.results.to_csv(path / "prediction.csv")
        return y_pred
