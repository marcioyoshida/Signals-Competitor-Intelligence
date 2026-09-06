"""ADR 022 Phase 5 — provision the scale-to-zero FinBERT-PT-BR SageMaker endpoint.

Deploys `lucas-leme/FinBERT-PT-BR` on a SageMaker **HuggingFace-DLC Serverless Inference** endpoint
(no docker build, no idle cost — pay per invoke). The monthly `OncaFinancialsPipeline` ToneTask
(`src/synth/financial_tone.lambda_handler`) invokes it via `ONCA_FINBERT_ENDPOINT`.

Prereqs / gotchas (learned the hard way on this box):
  - **SageMaker SDK v2** (`pip install "sagemaker<3"`). v3 dropped the `image_uris`/`HuggingFaceModel`
    API used here.
  - On **Python 3.14** the SDK's transitive `python-rapidjson` has no cp314 wheel → it compiles from
    source and needs the **Python dev headers** (`Python.h`). If `apt install python3.14-dev` needs
    sudo you don't have, download+extract the .deb and point the compiler at it:
        apt-get download python3.14-dev libpython3.14-dev && dpkg-deb -x <deb> ext/
        export CPLUS_INCLUDE_PATH=.../ext/usr/include/python3.14:.../ext/usr/include/x86_64-linux-gnu/python3.14
  - Serverless memory is account-quota-capped (was **3072 MB** here — enough for BERT-base;
    the default `deploy` asked 6144 and hit ResourceLimitExceeded).
  - IAM: an execution role (here `OncaSageMakerFinBERT`, trust sagemaker.amazonaws.com +
    AmazonSageMakerFullAccess).

Run (once):  SAGEMAKER_SUPPRESS_V2_WARNING=1 .venv/bin/python scripts/deploy_finbert_endpoint.py
Then the CDK default `ONCA_FINBERT_ENDPOINT=onca-finbert-ptbr` (infra/app.py) wires the ToneTask.
"""
import argparse

ROLE = "arn:aws:iam::668449743071:role/OncaSageMakerFinBERT"
ENDPOINT = "onca-finbert-ptbr"
MODEL_ID = "lucas-leme/FinBERT-PT-BR"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=ENDPOINT)
    ap.add_argument("--role", default=ROLE)
    ap.add_argument("--memory-mb", type=int, default=3072)   # account serverless quota cap
    ap.add_argument("--max-concurrency", type=int, default=3)
    args = ap.parse_args()

    from sagemaker.huggingface import HuggingFaceModel
    from sagemaker.serverless import ServerlessInferenceConfig

    model = HuggingFaceModel(
        role=args.role,
        transformers_version="4.49.0", pytorch_version="2.6.0", py_version="py312",
        env={"HF_MODEL_ID": MODEL_ID, "HF_TASK": "text-classification"},
    )
    print(f"deploying serverless endpoint {args.endpoint} ({args.memory_mb}MB, scale-to-zero)…")
    model.deploy(
        serverless_inference_config=ServerlessInferenceConfig(
            memory_size_in_mb=args.memory_mb, max_concurrency=args.max_concurrency),
        endpoint_name=args.endpoint, wait=True,
    )
    print("InService:", args.endpoint)


if __name__ == "__main__":
    main()
