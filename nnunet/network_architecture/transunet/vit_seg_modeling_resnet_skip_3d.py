import math

from os.path import join as pjoin
from collections import OrderedDict

import torch
import torch.nn as nn
import torch.nn.functional as F


def np2th(weights, conv=False):
    """Possibly convert HWIO to OIHW."""
    if conv:
        weights = weights.transpose([3, 2, 0, 1])
    return torch.from_numpy(weights)


class StdConv3d(nn.Conv3d):

    def forward(self, x):
        w = self.weight
        v, m = torch.var_mean(w, dim=[1, 2, 3, 4], keepdim=True, unbiased=False)
        w = (w - m) / torch.sqrt(v + 1e-5)
        return F.conv3d(x, w, self.bias, self.stride, self.padding,
                        self.dilation, self.groups)


def conv3x3_3d(cin, cout, stride=1, groups=1, bias=False):
    return StdConv3d(cin, cout, kernel_size=3, stride=stride,
                     padding=1, bias=bias, groups=groups)


def conv1x1_3d(cin, cout, stride=1, bias=False):
    return StdConv3d(cin, cout, kernel_size=1, stride=stride,
                     padding=0, bias=bias)


class PreActBottleneck_3d(nn.Module):
    """Pre-activation (v2) bottleneck block.
    """

    def __init__(self, cin, cout=None, cmid=None, stride=1):
        super().__init__()
        cout = cout or cin
        cmid = cmid or cout//4

        self.gn1 = nn.GroupNorm(32, cmid, eps=1e-6)
        self.conv1 = conv1x1_3d(cin, cmid, bias=False)
        self.gn2 = nn.GroupNorm(32, cmid, eps=1e-6)
        self.conv2 = conv3x3_3d(cmid, cmid, stride, bias=False)  # Original code has it on conv1!!
        self.gn3 = nn.GroupNorm(32, cout, eps=1e-6)
        self.conv3 = conv1x1_3d(cmid, cout, bias=False)
        self.relu = nn.ReLU(inplace=True)

        if (stride != 1 or cin != cout):
            # Projection also with pre-activation according to paper.
            self.downsample = conv1x1_3d(cin, cout, stride, bias=False)
            self.gn_proj = nn.GroupNorm(cout, cout)

    def forward(self, x):

        # Residual branch
        residual = x
        if hasattr(self, 'downsample'):
            residual = self.downsample(x)
            residual = self.gn_proj(residual)

        # Unit's branch
        y = self.relu(self.gn1(self.conv1(x)))
        y = self.relu(self.gn2(self.conv2(y)))
        y = self.gn3(self.conv3(y))

        y = self.relu(residual + y)
        return y


class ResNetV2_3D(nn.Module):
    """Implementation of Pre-activation (v2) ResNet 3D"""

    def __init__(self, block_units, width_factor, cin=1):
        super().__init__()
        width = int(64 * width_factor)
        self.width = width

        self.root = nn.Sequential(OrderedDict([
            ('conv', StdConv3d(cin, width, kernel_size=7, stride=2, bias=False, padding=3)),
            ('gn', nn.GroupNorm(32, width, eps=1e-6)),
            ('relu', nn.ReLU(inplace=True)),
            # ('pool', nn.MaxPool2d(kernel_size=3, stride=2, padding=0))
        ]))

        self.body = nn.Sequential(OrderedDict([
            ('block1', nn.Sequential(OrderedDict(
                [('unit1', PreActBottleneck_3d(cin=width, cout=width*4, cmid=width))] +
                [(f'unit{i:d}', PreActBottleneck_3d(cin=width*4, cout=width*4, cmid=width)) for i in range(2, block_units[0] + 1)],
                ))),
            ('block2', nn.Sequential(OrderedDict(
                [('unit1', PreActBottleneck_3d(cin=width*4, cout=width*8, cmid=width*2, stride=2))] +
                [(f'unit{i:d}', PreActBottleneck_3d(cin=width*8, cout=width*8, cmid=width*2)) for i in range(2, block_units[1] + 1)],
                ))),
            ('block3', nn.Sequential(OrderedDict(
                [('unit1', PreActBottleneck_3d(cin=width*8, cout=width*16, cmid=width*4, stride=2))] +
                [(f'unit{i:d}', PreActBottleneck_3d(cin=width*16, cout=width*16, cmid=width*4)) for i in range(2, block_units[2] + 1)],
                ))),
        ]))

    def forward(self, x):
        features = []
        b = x.shape[0]
        original_spatial_shape = x.shape[2:]
        x = self.root(x)
        features.append(x)
        x = nn.MaxPool3d(kernel_size=3, stride=2, padding=0)(x)
        for i in range(len(self.body)-1):
            x = self.body[i](x)
            feat = self.ensure_right_shape(x, b, original_spatial_shape, i)
            features.append(feat)
        x = self.body[-1](x)
        return x, features[::-1]

    def ensure_right_shape(self, x, b, original_spatial_shape, i):
        right_spatial_shape = torch.tensor(original_spatial_shape) / 4 / (i + 1)
        right_spatial_shape = right_spatial_shape.floor().to(torch.int)
        current_spatial_shape = torch.tensor(x.shape[2:]).to(torch.int)
        if (current_spatial_shape != right_spatial_shape).any():
            pad = right_spatial_shape - current_spatial_shape
            assert ((pad <= 3) & (pad >= 0)).all(), f"x {x.shape} should {right_spatial_shape}"
            feat = torch.zeros(
                (b, x.shape[1], *right_spatial_shape.tolist()),
                device=x.device)
            feat[:, :, :x.shape[2], :x.shape[3], :x.shape[4]] = x
        else:
            feat = x
        return feat
        # if x.size()[2] != right_size:
        #     pad = right_size - x.shape[-1]
        #     assert pad < 3 and pad > 0, "x {} should {}".format(x.size(), right_size)
        #     feat = torch.zeros((b, x.shape[1], x.shape[2], right_size, right_size), device=x.device)
        #     feat[:, :, 0:x.size()[2], 0:x.size()[3]] = x[:]
        # else:
        #     feat = x


if __name__ == "__main__":
    backbone = ResNetV2_3D((3, 4, 9), 1, 1).cuda()
    x = torch.rand(1, 1, 90, 192, 192).cuda()
    y, fts = backbone(x)
    print(y.shape)
    for ft in fts:
        print(ft.shape)
    print("hold")