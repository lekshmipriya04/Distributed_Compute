"""Train a scikit-learn model; each task tries one value of C.

Run it with one task per value, for example inputs 0.1, 1, 10. Each task
writes its score and its model to outputs/, which you download afterwards.
"""
import json
import os
import sys
import time

import joblib
from sklearn.datasets import load_digits
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

C = float(sys.stdin.read().strip() or "1.0")
X, y = load_digits(return_X_y=True)  # ships with scikit-learn: no download
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=0)

started = time.time()
model = make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=2000))
model.fit(X_train, y_train)
accuracy = model.score(X_test, y_test)
print(f"C={C:g}  accuracy={accuracy:.4f}  trained in {time.time() - started:.1f}s")

out = os.environ.get("DC_OUTPUT_DIR", "outputs")
os.makedirs(out, exist_ok=True)
joblib.dump(model, os.path.join(out, "model.joblib"))
with open(os.path.join(out, "metrics.json"), "w") as fh:
    json.dump({"C": C, "accuracy": accuracy}, fh, indent=2)
