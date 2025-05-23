import sys
import torch
from torch import nn
import torchvision
import numpy as np
import tqdm
import math
from ff import R2FeatureField
from utils import get_device, rmse
from group_basis import LocalGroupBasis
from atlasd import Predictor, LocalTrainer
from config import Config


device = get_device()

EXCLUSION_X = [0.1, 0.3]
EXCLUSION_Y = [0.2, 0.5]

IN_RAD1 = 14
OUT_RAD1 = 10
ATLAS1 = [
                               (0.5, 0.15),  (0.675, 0.15),  (0.85, 0.15),
                               (0.5, 0.325), (0.675, 0.325), (0.85, 0.325),
                               (0.5, 0.5),  (0.675, 0.5),  (0.85, 0.5),
    (0.15, 0.675), (0.325, 0.675), (0.5, 0.675), (0.675, 0.675), (0.85, 0.675),
    (0.15, 0.85),  (0.325, 0.85),  (0.5, 0.85),  (0.675, 0.85),  (0.85, 0.85),
]
IN_RAD2 = 26
OUT_RAD2 = 20
ATLAS2 = [
    (0.65, 0.3),
    (0.675, 0.625),
    (0.35, 0.75),
]

def heat_pde(x_in, boundary, boundary_val, alpha=1, dx=0.1, dt=0.01, t_steps=50):
    x_in[boundary] = boundary_val

    for _ in range(t_steps):
        x = torch.nn.functional.pad(x_in, (2, 2, 2, 2), value=boundary_val)

        ddx = (x[..., 2:, 1:-1] - x[..., :-2, 1:-1]) / (2 * dx)
        ddy = (x[..., 1:-1, 2:] - x[..., 1:-1, :-2]) / (2 * dx)

        dddx = (ddx[..., 2:, 1:-1] - ddx[..., :-2, 1:-1]) / (2 * dx)
        dddy = (ddy[..., 1:-1, 2:] - ddy[..., 1:-1, :-2]) / (2 * dx)

        ddt = (dddx + dddy) * alpha
        
        x_in = x_in + dt * ddt
        x_in[boundary] = boundary_val

    return x_in

class SinglePredictor(nn.Module):
    def __init__(self):
        super().__init__()

        self.model = nn.Sequential(
            nn.Conv2d(1, 16, kernel_size=3, padding='same'),
            nn.LeakyReLU(),
            nn.BatchNorm2d(16),

            nn.Conv2d(16, 16, kernel_size=3, padding='same'),
            nn.LeakyReLU(),
            nn.BatchNorm2d(16),

            nn.Conv2d(16, 16, kernel_size=3, padding='same'),
            nn.LeakyReLU(),
            nn.BatchNorm2d(16),

            nn.Conv2d(16, 1, kernel_size=3, padding='same'),
        ).to(device)


    def forward(self, x):
        return self.model(x)


class PDEFeatureField(R2FeatureField):
    def __init__(self, data):
        global config
        super().__init__(data) 

        c = self.data.shape[-1]
        r = self.data.shape[-2]

        atlas = ATLAS1 if config.atlas == 1 else ATLAS2
        self.locs = [(int(u * r), int(v * c)) for u, v in atlas]


class PDEPredictor(nn.Module, Predictor):
    def __init__(self):
        global config
        super().__init__()
        
        atlas = ATLAS1 if config.atlas == 1 else ATLAS2
        self.predictors = torch.nn.ModuleList([SinglePredictor() for _ in range(len(atlas))])
        self.optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)   

    def run(self, x):
        chart_ret = []
        for i, net in enumerate(self.predictors):
            ret = net(x[:, i])
            chart_ret.append(ret)

        return torch.stack(chart_ret, dim=1)

    def forward(self, x):
        return self.run(x)

    def loss(self, y_pred, y_true):
        return (y_pred - y_true).abs().mean()

    def batched_loss(self, y_pred, y_true):
        return (y_pred - y_true).flatten(1).abs().mean(dim=1)

    def name(self):
        return "pde"

    def needs_training(self):
        return True

    def returns_logits(self):
        return False


class PDEDataset(torch.utils.data.Dataset):
    def __init__(self, N, seed=0):
        super().__init__()

        torch.manual_seed(seed)

        a = 6 * torch.randn(N, 1, 1, device=device) + 15
        b = 3 * torch.randn(N, 1, 1, device=device)
        c = (torch.randn(N, 1, 1, device=device).abs() + 1) / 2

        d = 5 * torch.randn(N, 1, 1, device=device) + 12
        e = 3 * torch.randn(N, 1, 1, device=device)
        f = (torch.randn(N, 1, 1, device=device).abs() + 1) / 2

        rmax = 128
        r = torch.arange(rmax, device=device)
        u, v = torch.meshgrid(r, r, indexing='ij')
        u = u.unsqueeze(0).tile(N, 1, 1).float() / rmax
        v = v.unsqueeze(0).tile(N, 1, 1).float() / rmax

        self.X = (c * torch.sin(a * u + b) + f * torch.cos(d * v + e)).unsqueeze(1)

        boundary = (EXCLUSION_X[0] < u) & (u < EXCLUSION_X[1]) & (EXCLUSION_Y[0] < v) & (v < EXCLUSION_Y[1])
        self.Y = heat_pde(self.X, boundary.unsqueeze(1), math.sqrt(2))

    def __len__(self):
        return len(self.X)

    def __getitem__(self, idx):
        return self.X[idx], self.Y[idx]


def discover(config, algebra, cosets):
    targets = []
    if algebra:
        targets.append("algebra")
    if cosets:
        targets.append("cosets")

    print("Task: discovering", targets)

    predictor = PDEPredictor()
    if config.reuse_predictor:
        predictor.load_state_dict(torch.load("predictors/pde.pt", weights_only=True, map_location=device))

    in_rad = IN_RAD1 if config.atlas == 1 else IN_RAD2
    out_rad = OUT_RAD1 if config.atlas == 1 else OUT_RAD2

    basis = LocalGroupBasis(
        1, 2, 1, 1, config.standard_basis, 
        in_rad=in_rad, out_rad=out_rad, 
        num_cosets=16,
        identity_in_rep=True,
        identity_out_rep=True, 
        r3=0.1
    )

    dataset = PDEDataset(config.N)

    gdn = LocalTrainer(PDEFeatureField, predictor, basis, dataset, config)   

    if algebra:
        gdn.train()

    if cosets:
        if config.atlas == 1:
            lie = torch.tensor([[[-0.027137317, -1.000092626],
                    [ 1.016282678,  0.029424636]]], device=device)
        else:
            lie = torch.tensor([[[-0.182206243, -1.000181198],
                    [ 1.024214745,  0.188963503]]], device=device)

        gdn.discover_cosets(lie, 8)

if __name__ == '__main__':
    config = Config()

    if config.task == 'discover':
        discover(config, True, True)
    elif config.task == 'discover_algebra':
        discover(config, True, False)
    elif config.task == 'discover_cosets':
        discover(config, False, True)
    else:
        print("Unknown task for PDE")
