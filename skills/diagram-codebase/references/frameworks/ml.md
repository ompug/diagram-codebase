# Machine learning

PyTorch, TensorFlow/Keras, JAX, scikit-learn, and the code around them.

## Look for

- **Two pipelines**: training (data → model → loss → optimizer → checkpoint) and
  inference/serving (request or batch → preprocess → model → postprocess →
  output). They usually share preprocessing and the model class; find both and
  keep them as separate flows.
- **Entry points**: `train.py`, `evaluate.py`, `predict.py`, Hydra/argparse CLIs,
  Lightning `Trainer.fit`, HF `Trainer`, notebooks (cite as `static_inferred` at
  most; they rarely define production wiring).
- **Data**: `Dataset`/`DataLoader`, `tf.data`, HF `datasets`, transforms and
  augmentations (train-only vs shared), feature engineering, splits.
- **Model**: `nn.Module`/`keras.Model` definitions and their submodules; which
  config selects the architecture.
- **Training loop**: forward, loss, backward, optimizer/scheduler step,
  validation cadence, early stopping, gradient accumulation, distributed setup
  (DDP, `accelerate`).
- **Artifacts**: checkpoints (`torch.save`, `save_pretrained`), exported models
  (ONNX, TorchScript, SavedModel), metrics/logging (TensorBoard, W&B, MLflow).
- **Serving**: load checkpoint → model in eval mode → batching → API endpoint.

## Map to the model

| Code | Node / edge |
|---|---|
| Dataset / loader | `data_artifact` `data:<name>` (input role); `data_flow` into preprocessing |
| Preprocess / augment | `dataflows` stage `transform`; representation = tensor shape/dtype if visible |
| Model class | `algorithm` `algo:<model>` (or the scanned `cls:`), with forward stages if central |
| Training loop | `algorithms` entry: stages for batch, forward, loss, backward, step, validate; decision for early stop/epochs |
| Checkpoint / exported model | `data_artifact` `data:checkpoint`, `datastore` `store:<bucket or dir>` + `db_write` |
| Experiment tracker | `external_service` `ext:wandb` etc. + `external_call` |
| Serving endpoint | `api_endpoint` → inference flow |

## Pitfalls

- Config-driven architectures (Hydra, YAML registries): the chosen model depends
  on config; cite the registry and the default config, `static_inferred`.
- Tensor shapes are rarely explicit; write a representation only when the code
  states it.
- Notebook and script experiments are not the production pipeline unless
  something production-side calls them.
- Train-time augmentation must not appear in the inference flow.
- `model.eval()`/`torch.no_grad()` mark inference; their absence in serving code
  is worth an uncertainty, not a silent assumption.
