from abc import ABC, abstractmethod
import math
from functools import partial
import torch
from torch.nn import Sequential as Seq, Linear as Lin, ReLU, LeakyReLU, BatchNorm1d as BN, Dropout
from torch_geometric.nn import knn_interpolate, fps, radius, global_max_pool, global_mean_pool, knn
from torch_geometric.data import Batch


def copy_from_to(data, batch):
    for key in data.keys:
        if key not in batch.keys:
            setattr(batch, key, getattr(data, key, None))


def MLP(channels, activation=ReLU()):
    return Seq(*[
        Seq(Lin(channels[i - 1], channels[i]), activation, BN(channels[i]))
        for i in range(1, len(channels))
    ])

################## BASE CONVOLUTION #####################


class BaseConvolution(ABC, torch.nn.Module):

    def __init__(self, sampler, neighbour_finder, *args, **kwargs):
        torch.nn.Module.__init__(self)

        self.sampler = sampler
        self.neighbour_finder = neighbour_finder


class BaseConvolutionDown(BaseConvolution):
    def __init__(self, sampler, neighbour_finder, *args, **kwargs):
        super(BaseConvolutionDown, self).__init__(sampler, neighbour_finder, *args, **kwargs)

        self._precompute_multi_scale = kwargs.get("precompute_multi_scale", None)
        self._index = kwargs.get("index", None)

    def conv(self, x, pos, edge_index):
        raise NotImplementedError

    def forward(self, data):
        batch_obj = Batch()
        x, pos, batch = data.x, data.pos, data.batch
        if self._precompute_multi_scale:
            idx = getattr(data, "idx_{}".format(self._index), None)
            edge_index = getattr(data, "edge_index_{}".format(self._index), None)
        else:
            idx = self.sampler(pos, batch)
            row, col = self.neighbour_finder(pos, pos[idx], batch, batch[idx])
            edge_index = torch.stack([col, row], dim=0)
        batch_obj.x = self.conv(x, (pos, pos[idx]), edge_index)
        batch_obj.pos = pos[idx]
        batch_obj.batch = batch[idx]
        copy_from_to(data, batch_obj)
        return batch_obj


class BaseConvolutionUp(BaseConvolution):
    def __init__(self, neighbour_finder, *args, **kwargs):
        super(BaseConvolutionUp, self).__init__(None, neighbour_finder, *args, **kwargs)

        self._precompute_multi_scale = kwargs.get("precompute_multi_scale", None)
        self._index = kwargs.get("index", None)
        self._skip = kwargs.get("skip", True)

    def conv(self, x, pos, pos_skip, batch, batch_skip, edge_index):
        raise NotImplementedError

    def forward(self, data):
        batch_obj = Batch()
        data, data_skip = data
        x, pos, batch = data.x, data.pos, data.batch
        x_skip, pos_skip, batch_skip = data_skip.x, data_skip.pos, data_skip.batch

        if self.neighbour_finder is not None:
            if self._precompute_multi_scale:  # TODO For now, it uses the one calculated during down steps
                edge_index = getattr(data_skip, "edge_index_{}".format(self._index), None)
                col, row = edge_index
                edge_index = torch.stack([row, col], dim=0)
            else:
                row, col = self.neighbour_finder(pos, pos_skip, batch, batch_skip)
                edge_index = torch.stack([col, row], dim=0)
        else:
            edge_index = None

        x = self.conv(x, pos, pos_skip, batch, batch_skip, edge_index)

        if x_skip is not None and self._skip:
            x = torch.cat([x, x_skip], dim=1)
        batch_obj.x = self.nn(x)
        copy_from_to(data_skip, batch_obj)
        return batch_obj


class GlobalBaseModule(torch.nn.Module):
    def __init__(self, nn, aggr='max'):
        super(GlobalBaseModule, self).__init__()
        self.nn = MLP(nn)
        self.pool = global_max_pool if aggr == "max" else global_mean_pool

    def forward(self, data):
        batch_obj = Batch()
        x, pos, batch = data.x, data.pos, data.batch
        x = self.nn(torch.cat([x, pos], dim=1))
        x = self.pool(x, batch)
        batch_obj.x = x
        batch_obj.pos = pos.new_zeros((x.size(0), 3))
        batch_obj.batch = torch.arange(x.size(0), device=batch.device)
        copy_from_to(data, batch_obj)
        return batch_obj

#################### COMMON MODULE ########################


class FPModule(BaseConvolutionUp):
    """ Upsampling module from PointNet++

    Arguments:
        k [int] -- number of nearest neighboors used for the interpolation
        up_conv_nn [List[int]] -- list of feature sizes for the uplconv mlp

    Returns:
        [type] -- [description]
    """

    def __init__(self, up_k, up_conv_nn, *args, **kwargs):
        super(FPModule, self).__init__(None)
        self.k = up_k
        self.nn = MLP(up_conv_nn)

    def conv(self, x, pos, pos_skip, batch, batch_skip, *args):
        return knn_interpolate(x, pos, pos_skip, batch, batch_skip, k=self.k)
