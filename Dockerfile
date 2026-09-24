# Build on the deployment host/CI after dependency and redistribution approval.
# Runtime never downloads embedding/reranker weights.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    ANONYMIZED_TELEMETRY=False \
    MPLCONFIGDIR=/tmp/matplotlib \
    REPORT_CJK_FONT=/usr/share/fonts/truetype/noto/NotoSansSC-VF.ttf \
    HF_HOME=/tmp/huggingface \
    COST_AUTH_MODE=oidc \
    COST_TENANT_ID=default \
    COST_DATA_DIR=/var/lib/project4/data \
    COST_MANAGED_DIR=/var/lib/project4/managed \
    COST_LLM_CONFIG_FILE=/var/lib/project4-private/llm.json \
    BGE_M3_PATH=/models/bge-m3 \
    BGE_RERANKER_PATH=/models/bge-reranker-v2-m3

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 fonts-noto-cjk fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 costapp \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp costapp

WORKDIR /app
# WQY lacks U+2212. Install an unmodified OFL TrueType font with both resources
# pinned by immutable upstream commit, byte length and SHA256. Runtime is offline.
COPY deploy/install_cjk_font.py /app/deploy/install_cjk_font.py
RUN python /app/deploy/install_cjk_font.py
ARG INSTALL_LOCAL_MODELS=true
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
COPY requirements.txt requirements-models.txt /app/
RUN python -m pip install --no-cache-dir -r /app/requirements.txt
# Install the platform torch wheel first, then BGE's remaining runtime.
# Model weights are always supplied separately through the optional volume.
RUN if [ "$INSTALL_LOCAL_MODELS" = "true" ]; then \
        sed -n '/^torch==/p' /app/requirements-models.txt > /tmp/torch-requirements.txt \
        && python -m pip install --no-cache-dir --index-url "$TORCH_INDEX_URL" -r /tmp/torch-requirements.txt \
        && python -m pip install --no-cache-dir -r /app/requirements-models.txt \
        && rm /tmp/torch-requirements.txt; \
    elif [ "$INSTALL_LOCAL_MODELS" = "false" ]; then \
        echo "Local BGE dependencies omitted; semantic retrieval requires another image build."; \
    else \
        echo "INSTALL_LOCAL_MODELS must be true or false" >&2; exit 1; \
    fi

# .dockerignore uses an allowlist: competition files, databases and secrets never
# enter the build context. Public examples are the exact-hash SIMULATION exception.
COPY *.py /app/
COPY enterprise/ /app/enterprise/
COPY app_pages/ /app/app_pages/
COPY dashboard/ /app/dashboard/
COPY report/ /app/report/
# Reviewed non-secret semantics only; never COPY arbitrary customer configuration.
COPY config/domain_profiles/pharma.json /app/config/domain_profiles/pharma.json
COPY config/manufacturing_adapters/machinery.json config/manufacturing_adapters/auto_parts.json config/manufacturing_adapters/chemicals.json config/manufacturing_adapters/electronics.json /app/config/manufacturing_adapters/
COPY rag_fixed_v1/ /app/rag_fixed_v1/
COPY scripts/ /app/scripts/
# .dockerignore lists each reviewed example; no wildcard CSV/data admission.
COPY config/manufacturing_examples/ /app/config/manufacturing_examples/
COPY README.md /app/README.md
COPY docs/跨行业迁移与边界.md docs/第二轮核查实施与运行说明.md docs/受控散文生成与阅读导出.md /app/docs/
# Do not import the application or load private config while checking public bytes.
RUN python -c "from scripts.build_source_bundle import ROOT, REVIEWED_SIMULATION_HASHES, read_regular, scan_text, validate_reviewed_simulations; s = {n: read_regular(ROOT / n) for n in REVIEWED_SIMULATION_HASHES}; validate_reviewed_simulations(s); assert not any(scan_text(n, b)[0] for n, b in s.items()), 'Public simulation text scan failed'"
COPY assets/ /app/assets/
COPY deploy/entrypoint.py deploy/healthcheck.py deploy/generate_inventory.py deploy/container_smoke.py /app/deploy/
COPY deploy/streamlit.config.toml /app/.streamlit/config.toml
RUN mkdir -p /var/lib/project4/data /var/lib/project4/managed /var/lib/project4-private /models \
    && chown -R 10001:10001 /var/lib/project4 /var/lib/project4-private /models

USER 10001:10001
EXPOSE 8000 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "deploy/healthcheck.py"]
ENTRYPOINT ["python", "deploy/entrypoint.py"]
