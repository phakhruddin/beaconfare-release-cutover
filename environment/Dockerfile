FROM --platform=linux/amd64 mirror.gcr.io/library/python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS base

ARG TERRAFORM_VERSION=1.15.8
ARG AWS_PROVIDER_VERSION=6.51.0

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AWS_ACCESS_KEY_ID=test \
    AWS_SECRET_ACCESS_KEY=test \
    AWS_DEFAULT_REGION=us-east-1 \
    AWS_ENDPOINT_URL=http://aws:4566 \
    TF_CLI_CONFIG_FILE=/etc/terraformrc.offline

COPY vendor/ /opt/offline/
RUN dpkg --unpack /opt/offline/debs/*.deb \
    && dpkg --configure -a \
    && pip install --no-index --find-links=/opt/offline/wheels awscli boto3 requests jsonschema pyyaml pytest zstandard \
    && python -m zipfile -e /opt/offline/terraform.zip /usr/local/bin \
    && mkdir -p /opt/terraform-provider-mirror/registry.terraform.io/hashicorp/aws/${AWS_PROVIDER_VERSION}/linux_amd64 \
    && cat /opt/offline/beaconfare-aws-provider-zst.part-00 /opt/offline/beaconfare-aws-provider-zst.part-01 > /tmp/aws-provider.zst \
    && python -c 'import zstandard,sys; open(sys.argv[2], "wb").write(zstandard.ZstdDecompressor().decompress(open(sys.argv[1], "rb").read()))' /tmp/aws-provider.zst /opt/terraform-provider-mirror/registry.terraform.io/hashicorp/aws/${AWS_PROVIDER_VERSION}/linux_amd64/terraform-provider-aws_v${AWS_PROVIDER_VERSION}_x5 \
    && chmod 0755 /opt/terraform-provider-mirror/registry.terraform.io/hashicorp/aws/${AWS_PROVIDER_VERSION}/linux_amd64/terraform-provider-aws_v${AWS_PROVIDER_VERSION}_x5 \
    && printf '%s\n' \
      'disable_checkpoint = true' \
      'provider_installation {' \
      '  filesystem_mirror {' \
      '    path = "/opt/terraform-provider-mirror"' \
      '    include = ["registry.terraform.io/hashicorp/aws"]' \
      '  }' \
      '}' >/etc/terraformrc.offline \
    && chmod 0755 /usr/local/bin/terraform \
    && rm /tmp/aws-provider.zst \
    && for tool in bash curl jq terraform aws python3; do command -v "$tool" >/dev/null; done

COPY workspace/ /workspace/
RUN chmod -R a+rX /workspace

RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash agent \
    && mkdir -p /workspace/config /workspace/submission /workspace/evidence \
    && chown -R agent:agent /workspace/submission /workspace/evidence

WORKDIR /workspace
USER agent
