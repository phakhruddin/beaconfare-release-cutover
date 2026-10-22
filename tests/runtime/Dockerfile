# Two targets: a thin Docker CLI image that builds the supplied application
# images and writes the per-run configuration, and the verifier itself.

FROM mirror.gcr.io/library/docker:27.5.1-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c AS runtime-setup
COPY application/ /application/
COPY runtime/runtime.sh /runtime.sh


FROM --platform=linux/amd64 mirror.gcr.io/library/python:3.12-slim AS verifier
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/tests \
    TF_CLI_CONFIG_FILE=/etc/terraformrc.offline

COPY vendor/wheels/ /opt/offline/wheels/
COPY vendor/debs/ /opt/offline/debs/
COPY vendor/terraform.zip /opt/offline/terraform.zip
COPY vendor/provider/ /opt/offline/provider/
RUN dpkg --unpack /opt/offline/debs/*.deb \
    && dpkg --configure -a \
    && pip install --no-index --find-links=/opt/offline/wheels awscli boto3 requests jsonschema pyyaml pytest zstandard \
    && python -m zipfile -e /opt/offline/terraform.zip /usr/local/bin \
    && mkdir -p /opt/terraform-provider-mirror/registry.terraform.io/hashicorp/aws/6.51.0/linux_amd64 \
    && cat /opt/offline/provider/beaconfare-aws-provider-zst.part-00 /opt/offline/provider/beaconfare-aws-provider-zst.part-01 > /tmp/aws-provider.zst \
    && python -c 'import zstandard,sys; open(sys.argv[2], "wb").write(zstandard.ZstdDecompressor().decompress(open(sys.argv[1], "rb").read()))' /tmp/aws-provider.zst /opt/terraform-provider-mirror/registry.terraform.io/hashicorp/aws/6.51.0/linux_amd64/terraform-provider-aws_v6.51.0_x5 \
    && printf '%s\n' \
      'disable_checkpoint = true' \
      'provider_installation {' \
      '  filesystem_mirror {' \
      '    path = "/opt/terraform-provider-mirror"' \
      '    include = ["registry.terraform.io/hashicorp/aws"]' \
      '  }' \
      '}' >/etc/terraformrc.offline \
    && chmod 0755 /usr/local/bin/terraform \
    && chmod 0755 /opt/terraform-provider-mirror/registry.terraform.io/hashicorp/aws/6.51.0/linux_amd64/terraform-provider-aws_v6.51.0_x5 \
    && rm /tmp/aws-provider.zst \
    && for tool in aws bash curl jq terraform timeout python3; do command -v "$tool" >/dev/null; done

COPY . /tests
COPY contracts/ /workspace/contracts/
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
