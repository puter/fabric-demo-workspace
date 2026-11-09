# %% [markdown]
# # Bronze ingest — Eventhouse ➜ `bronze_events`
#
# Incrementally copies new rows from `kqldb_mvoi.Events` into the Lakehouse,
# using Eventhouse `ingestion_time()` as the watermark. Lossless: every column
# is kept, including the original source payload (`raw_json`) and replay rows
# (tagged `run_mode = 'replay'`), so history can be replayed or re-derived.
# Eventhouse keeps 30 days hot; Bronze keeps everything.

# %% tags=["parameters"]
lag_seconds = 60          # don't read rows still being ingested

# %%
# {{BOOTSTRAP}}

# %%
watermark = sj.bronze_watermark(spark, TABLES)
query = sj.bronze_kusto_query(watermark, lag_seconds)
print(query)

uri = RUNTIME["kql_query_uri"]
token = notebookutils.credentials.getToken(uri)  # noqa: F821 (Fabric builtin)
batch = (spark.read.format("com.microsoft.kusto.spark.synapse.datasource")
         .option("accessToken", token)
         .option("kustoCluster", uri)
         .option("kustoDatabase", RUNTIME["kql_database"])
         .option("kustoQuery", query)
         .load())

rows = sj.bronze_append(spark, batch, TABLES)
summary = sj.run_summary(step="bronze", rows=rows, previous_watermark=watermark,
                         new_watermark=sj.bronze_watermark(spark, TABLES))
print(summary)
notebookutils.notebook.exit(summary)  # noqa: F821
