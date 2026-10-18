"""A PyTorch training script, unchanged from how you would run it locally.

It uses the GPU when the worker lends one (CUDA or Apple MPS) and the CPU
otherwise, saves a checkpoint to outputs/, and reads its learning rate from
the task input so several tasks can try several rates at once.
"""
import json
import os
import sys
import time

import torch
from torch import nn

lr = float(sys.stdin.read().strip() or "0.01")
torch.manual_seed(0)
device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"

# Synthetic data so the example needs no download: y = sin(x0) + x1^2 + noise.
X = torch.rand(4096, 2) * 4 - 2
y = (torch.sin(X[:, 0]) + X[:, 1] ** 2 + 0.05 * torch.randn(4096)).unsqueeze(1)
X, y = X.to(device), y.to(device)

model = nn.Sequential(nn.Linear(2, 64), nn.ReLU(), nn.Linear(64, 64), nn.ReLU(), nn.Linear(64, 1)).to(device)
optimizer = torch.optim.Adam(model.parameters(), lr=lr)
loss_fn = nn.MSELoss()

started = time.time()
for epoch in range(1, 201):
    optimizer.zero_grad()
    loss = loss_fn(model(X), y)
    loss.backward()
    optimizer.step()
    if epoch % 50 == 0:
        print(f"epoch {epoch:3d}  loss {loss.item():.4f}", flush=True)

out = os.environ.get("DC_OUTPUT_DIR", "outputs")
os.makedirs(out, exist_ok=True)
torch.save(model.state_dict(), os.path.join(out, "model.pt"))
with open(os.path.join(out, "metrics.json"), "w") as fh:
    json.dump({"lr": lr, "loss": loss.item(), "device": device, "seconds": round(time.time() - started, 1)}, fh)
print(f"done on {device} in {time.time() - started:.1f}s")
