FROM python:3.12-slim

WORKDIR /app
COPY api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY api/main.py /app/main.py
COPY api/retail_entity_scope.py /app/retail_entity_scope.py
COPY api/retail_evidence_scope.py /app/retail_evidence_scope.py
RUN mkdir -p /app/retail_ops/outputs
COPY retail_ops/outputs/generated_demo2_retail_memory_facts.json /app/retail_ops/outputs/generated_demo2_retail_memory_facts.json

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
