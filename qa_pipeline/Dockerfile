# ADR 027 (#127): OncaQaPipeline's Lambda container image. Base = Microsoft's OFFICIAL
# Playwright Python runtime image — Chromium/Firefox/WebKit + all OS deps already installed,
# which is the entire reason this is a container image and not a zip/layer (the browser
# binaries alone exceed Lambda's 250 MB unzipped layer ceiling). Pinned to the exact
# Playwright version this repo's `requirements-dev.txt` uses (`playwright==1.62.0`) — a
# version mismatch between the pip package and the installed browser build is a hard
# failure at launch time, not a warning.
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble-amd64

WORKDIR /var/task

# This base image has no AWS Lambda runtime support — it's not one of AWS's own base
# images — so the Lambda Runtime Interface Client (awslambdaric) must be added explicitly.
# boto3 is likewise NOT runtime-provided here (unlike a native `public.ecr.aws/lambda/python`
# base), so it's a real dependency, not a courtesy pin.
COPY qa_pipeline/requirements-lambda.txt .
RUN pip install --no-cache-dir -r requirements-lambda.txt

COPY qa_pipeline/ ./qa_pipeline/

ENTRYPOINT ["python3", "-m", "awslambdaric"]
CMD ["qa_pipeline.handler.lambda_handler"]
