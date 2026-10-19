# Two targets: a thin Docker CLI image that builds the supplied application
# images and writes the per-run configuration, and the verifier itself.

FROM mirror.gcr.io/library/docker:27.5.1-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c AS runtime-setup
RUN apk add --no-cache coreutils
COPY tests/application/ /application/
COPY tests/runtime/runtime.sh /runtime.sh


FROM --platform=linux/amd64 mirror.gcr.io/library/python:3.12-slim AS verifier
ARG TERRAFORM_VERSION=1.15.8
ARG AWS_PROVIDER_VERSION=6.51.0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/tests \
    TF_CLI_CONFIG_FILE=/etc/terraformrc.offline

COPY environment/vendor/ /opt/offline/
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
    # The contracts promise the verifier has the same tools the agent had.
    # Fail the image build rather than a trial if that stops being true.
    && for tool in bash curl jq terraform aws python3; do command -v "$tool" >/dev/null; done

COPY tests/ /tests
COPY environment/workspace/contracts/ /workspace/contracts/
# Host file modes are not guaranteed to survive the copy; the verifier user
# must be able to read every test and contract.
RUN chmod -R a+rX /tests /workspace/contracts

RUN groupadd --gid 10001 verifier \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash verifier \
    && mkdir -p /logs/verifier /workspace/config /workspace/submission /workspace/evidence \
    && chown -R verifier:verifier /logs/verifier /workspace \
    && chmod 0555 /tests/test.sh

WORKDIR /tests
USER verifier
CMD ["sleep", "infinity"]
