from abc import ABC
from typing import Any, Sequence, Tuple, Union

import hydra
import numpy as np
import plotly.graph_objects as go
import pytorch_lightning as pl
import wandb
from plotly.graph_objs.layout import Annotation
from torch.optim import Optimizer
from torchmetrics import ConfusionMatrix


class MyLightningModule(pl.LightningModule, ABC):
    def __init__(self):
        super().__init__()

    def configure_optimizers(
        self,
    ) -> Union[Sequence[Optimizer], Tuple[Sequence[Optimizer], Sequence[Any]]]:
        """Choose what optimizers and learning-rate schedulers to use in your optimization.
        Normally you'd need one. But in the case of GANs or similar you might have multiple.
        Return:
            Any of these 6 options.
            - Single optimizer.
            - List or Tuple - List of optimizers.
            - Two lists - The first list has multiple optimizers, the second a list of LR schedulers (or lr_dict).
            - Dictionary, with an 'optimizer' key, and (optionally) a 'lr_scheduler'
              key whose value is a single LR scheduler or lr_dict.
            - Tuple of dictionaries as described, with an optional 'frequency' key.
            - None - Fit will run without any optimizer.
        """
        opt = hydra.utils.instantiate(self.hparams.optimizer, params=self.parameters(), _convert_="partial")
        if "lr_scheduler" not in self.hparams:
            return [opt]
        scheduler = hydra.utils.instantiate(self.hparams.lr_scheduler, optimizer=opt)
        return [opt], [scheduler]

    def log_metrics(self, split: str, on_step: bool, on_epoch: bool, cm_reset: bool):
        to_log = {}

        for metric_name, metric in getattr(self, f"{split}_metrics").items():
            if "none" in metric_name:
                self.handle_no_average_metric(metric_name, metric, to_log)
            elif "cm" in metric_name:
                self.handle_confusion_matrix(metric_name, metric)
                if cm_reset:
                    metric.reset()
            else:
                # TODO: fix, this is called in validation_step where the metrics are already computed
                #       step_wise, but also in on_train_batch_end where the metrics must be computed
                to_log[metric_name] = metric

        self.log_dict(to_log, on_step=on_step, on_epoch=on_epoch)

    def handle_no_average_metric(self, metric_name, metric, to_log):
        for label, score in list(
            zip(
                self.classes,
                metric.compute(),
            )
        ):
            to_log[f"{metric_name}/{label}"] = score
        metric.reset()

    def handle_confusion_matrix(self, cm_name, metric):
        fig: go.Figure = self.plot_cm(cm=metric)
        wandb.log(
            data={cm_name: fig},
        )

    def plot_cm(self, cm: ConfusionMatrix) -> go.Figure:
        z: np.ndarray = cm.compute().cpu().numpy()
        x = y = list(self.classes)
        class2index = {c: i for i, c in enumerate(self.classes)}

        hover_text = [[str(y) for y in x] for x in z]

        z = np.nan_to_num((z / z.sum(axis=1)).round(2))
        z_text = [[str(y) for y in x] for x in z]

        fig = go.Figure(
            data=go.Heatmap(
                z=z,
                text=z_text,
                x=x,
                y=y,
                customdata=hover_text,
                colorscale="Blues",
                zmin=0,
                zmax=1,
                hovertemplate="<br>".join(
                    (
                        "<b>Predicted</b>: %{y}",
                        "<b>Label</b>: %{x}",
                        "",
                        "<b>Row-normalized</b>: %{z:.3f}",
                        "<b>Original</b>: %{customdata}",
                    )
                ),
            )
        )

        annotations = []
        for n, row in enumerate(z):
            for m, val in enumerate(row):
                annotations.append(
                    Annotation(
                        text=str(z[n][m]),
                        x=class2index[x[m]],
                        y=class2index[y[n]],
                        xref="x1",
                        yref="y1",
                        showarrow=False,
                    )
                )

        fig.update_yaxes(autorange="reversed", type="category")
        fig.update_xaxes(type="category")

        fig.update_layout(
            font=dict(family="Courier New, monospace", size=20, color="black"),
            annotations=annotations,
            xaxis_title="Prediction",
            yaxis_title="Target",
        )

        return fig
