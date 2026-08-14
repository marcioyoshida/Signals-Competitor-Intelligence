# Phase 2, Stage A Validation Summary

## Status: ✅ COMPLETE AND VALIDATED

Stage A of Phase 2 has been successfully deployed and validated end-to-end. The corpus population pipeline is live, writing documents to S3 and syncing them into the Bedrock Knowledge Base.

## What Was Validated

### 1. Corpus Population (S3 Raw Objects)
- ✅ Documents are being written to `s3://onca-raw-668449743071/`
- ✅ Format: `{source}/{id}.txt` + `{source}/{id}.txt.metadata.json`
- ✅ Current count: 295+ documents indexed
- ✅ Metadata attributes preserved: `source`, `doc_type`, `url`, `date`, `kind`

### 2. Knowledge Base Ingestion
- ✅ Bedrock embedding quota increased from 0 to 60 RPM for Titan Text Embeddings V2
- ✅ Ingestion jobs completing successfully (7 recent jobs, all COMPLETE)
- ✅ Latest job: 4 new documents indexed on 2026-08-08
- ✅ Zero document failures across all ingestion runs

### 3. Retrieval with Citations
- ✅ Bedrock Retrieve API returning results with proper metadata
- ✅ Citation fields survive round-trip:
  - `source`: "BCB" or "CVM-Ofertas"
  - `doc_type`: "Instrução Normativa BCB", "Notas Comerciais", etc.
  - `url`: Original source URL
  - `date`: Document publication date
  - `kind`: "regulatory" or "competitor"
- ✅ Scores ranging from 0.65 to 0.75 for relevant queries

### 4. Lambda Integration
- ✅ Environment variables properly configured:
  - `ONCA_KB_ID`: CQ5LBZBQTY
  - `ONCA_KB_DATA_SOURCE_ID`: RXJWILH51Z
  - `ONCA_RAW_BUCKET`: onca-raw-668449743071
- ✅ Graceful degradation working (failures don't break digest flow)
- ✅ KB sync only triggered when new documents are written

## Test Results

### Retrieval Query: "BCB normativo Pix"
```json
{
  "retrievalResults": [
    {
      "content": {
        "text": "Instrução Normativa BCB N° 766  Divulga a versão 8.5 do Manual Operacional do Diretório de Identificadores de Contas Transacionais (DICT), que compõe o Regulamento do Pix.",
        "type": "TEXT"
      },
      "location": {
        "s3Location": {
          "uri": "s3://onca-raw-668449743071/BCB/bcb:Instrução Normativa BCB:766.txt"
        },
        "type": "S3"
      },
      "metadata": {
        "date": "2026-07-27",
        "kind": "regulatory",
        "source": "BCB",
        "doc_type": "Instrução Normativa BCB",
        "url": "https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?tipo=Instru%C3%A7%C3%A3o+Normativa+BCB&numero=766"
      },
      "score": 0.7549
    }
  ]
}
```

## Infrastructure Status

- **S3 Vectors Bucket**: Deployed and operational
- **Knowledge Base**: ACTIVE (CQ5LBZBQTY)
- **Data Source**: AVAILABLE (RXJWILH51Z)
- **Embedding Model**: amazon.titan-embed-text-v2:0 (60 RPM quota)
- **Lambda Function**: OncaPrototypeStack-OncaLambdaPrototype6DC6C2A9-cEyrm4m90PE0

## Cost Impact

- S3 Vectors storage: Minimal (tens of documents/day)
- Embedding usage: Well within 60 RPM quota
- Ingestion jobs: Low single-digit dollars/month
- **Total**: Well under the $100/month budget ceiling

## Next Steps (Phase 2, Stage B)

With Stage A validated and operational, the next phase is to implement the synthesis Lambda that:
1. Consumes the digest payload from Stage A
2. Uses Bedrock Retrieve + Converse APIs for correlation
3. Produces flagged narratives with source citations
4. Writes synthesized outputs for downstream delivery

See `docs/2026-07-19-phase2-stage-b-scaffold.md` for the Stage B design.

## Files Modified/Created (Stage A)

### New Files:
- `src/ingest/raw_writer.py`
- `tests/test_raw_writer.py`
- `docs/2026-07-12-phase2-stage-a-knowledge-base.md`
- `docs/STAGE_A_VALIDATION_SUMMARY.md` (this file)

### Modified Files:
- `src/ingest/lambda_port.py` - Added raw writer and KB sync logic
- `infra/app.py` - Added S3 Vectors, Knowledge Base, and IAM resources
- `CLAUDE.md` - Updated Phase 2 status to reflect Stage A completion
- `tests/test_lambda_port.py` - Added KB sync tests

## Validation Commands

```bash
# Check embedding quota
aws service-quotas get-service-quota \
  --service-code bedrock \
  --quota-code L-26C560CE \
  --region us-east-1 \
  --profile my2027

# List ingestion jobs
aws bedrock-agent list-ingestion-jobs \
  --knowledge-base-id CQ5LBZBQTY \
  --data-source-id RXJWILH51Z \
  --region us-east-1 \
  --profile my2027

# Test retrieval
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id CQ5LBZBQTY \
  --cli-input-json file:///tmp/retrieve_input.json \
  --region us-east-1 \
  --profile my2027

# Check S3 objects
aws s3 ls s3://onca-raw-668449743071/ --recursive | head -20
```

## Conclusion

✅ **Stage A is production-ready.** The corpus population pipeline successfully writes regulatory and competitor documents to S3 with proper metadata, syncs them into the Bedrock Knowledge Base via ingestion jobs, and enables citation-preserving retrieval. All validation criteria have been met.
