from typing import Any, Sequence, Tuple, Union

import higher
import hydra
import torch
import torch.nn.functional as F
from hydra.utils import instantiate
from torch.optim import Optimizer
from torchmetrics import Accuracy

from fs_grl.data.episode import EpisodeBatch
from fs_grl.modules.baselines.gnn_mlp import GNN_MLP
from fs_grl.pl_modules.meta_learning import MetaLearningModel


class MAMLModel(MetaLearningModel):
    def __init__(self, cfg, metadata, *args, **kwargs) -> None:
        super().__init__(metadata=metadata, *args, **kwargs)
        self.save_hyperparameters(logger=False, ignore=("metadata",))
        self.cfg = cfg

        self.gnn_mlp: GNN_MLP = instantiate(
            cfg.model,
            cfg=cfg.model,
            feature_dim=metadata.feature_dim,
            num_classes=self.metadata.num_classes,
            _recursive_=False,
        )

        self.inner_optimizer = instantiate(cfg.inner_optimizer, params=self.gnn_mlp.parameters())

        # self.train_inner_accuracy = metric.clone()
        # self.train_outer_accuracy = metric.clone()
        # self.val_inner_accuracy = metric.clone()
        # self.val_outer_accuracy = metric.clone()
        # self.test_inner_accuracy = metric.clone()
        # self.test_outer_accuracy = metric.clone()

    def forward(self, batch: EpisodeBatch) -> torch.Tensor:
        """
        :return similarities, tensor ~ (B*(N*Q)*N) containing for each episode the similarity
                between each of the N*Q queries and the N label prototypes
        """

        # model_out = self.model(batch)
        print("lmao")
        # return model_out

    def step(self, train: bool, batch: EpisodeBatch):
        self.gnn_mlp.zero_grad()
        outer_optimizer = self.optimizers()

        metric = Accuracy()
        outer_loss = torch.tensor(0.0)
        inner_loss = torch.tensor(0.0)
        outer_accuracy = metric.clone()
        inner_accuracy = metric.clone()

        # TODO: find out what to do here
        supports, queries = batch.supports, batch.queries

        for episode_idx, (episode_supports, episode_queries) in enumerate(zip(supports, queries)):
            track_higher_grads = True if train else False

            with higher.innerloop_ctx(
                self.gnn_mlp, self.inner_optimizer, copy_initial_weights=False, track_higher_grads=track_higher_grads
            ) as (fmodel, diffopt):

                for k in range(self.cfg.data.datamodule.num_inner_steps):
                    train_logit = fmodel(episode_supports)
                    loss = F.cross_entropy(train_logit, episode_supports.y)
                    diffopt.step(loss)

                with torch.no_grad():
                    train_logit = fmodel(episode_supports)
                    train_preds = torch.softmax(train_logit, dim=-1)
                    inner_loss += F.cross_entropy(train_logit, episode_supports.y)
                    inner_accuracy.update(train_preds.cpu(), episode_supports.y)

                test_logit = fmodel(episode_queries)
                outer_loss += F.cross_entropy(test_logit, episode_queries.y)
                with torch.no_grad():
                    test_preds = torch.softmax(train_logit, dim=-1)
                    outer_accuracy.update(test_preds.cpu(), episode_queries.y)

        if train:
            self.manual_backward(outer_loss, outer_optimizer)
            outer_optimizer.step()

        outer_loss.div_(episode_idx + 1)
        inner_loss.div_(episode_idx + 1)

        return outer_loss, inner_loss, outer_accuracy, inner_accuracy

    def configure_optimizers(
        self,
    ) -> Union[Optimizer, Tuple[Sequence[Optimizer], Sequence[Any]]]:
        outer_optimizer = hydra.utils.instantiate(self.cfg.outer_optimizer, params=self.parameters())

        if self.cfg.use_lr_scheduler:
            scheduler = hydra.utils.instantiate(self.cfg.lr_scheduler, optimizer=outer_optimizer)
            return [outer_optimizer], [scheduler]

        return outer_optimizer
