"""Local Spark session that mirrors the Databricks runtime closely enough for
Bronze/Silver/Gold logic to be developed and tested off-platform.

Matched deliberately:
  * Spark 3.5.x - same major/minor as DBR 15.4 / 16.4 LTS
  * session timeZone = UTC, and the driver process TZ with it - so a local run cannot
    accidentally pass a test that would fail on the cluster, and vice versa
    (README section 18)
  * Delta as the default catalog

Not available locally: Auto Loader (`cloudFiles`), Unity Catalog, Workflows. Those
four are the only reasons this project needs the cluster at all; everything else in
the pipeline is developed here at zero cost (docs/decisions.md D-12).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

JAVA_11 = "/usr/lib/jvm/java-11-openjdk-amd64"


def local_session(app: str = "energy-lakehouse", cores: str = "*"):
    from delta import configure_spark_with_delta_pip
    from pyspark.sql import SparkSession

    if "JAVA_HOME" not in os.environ and Path(JAVA_11).exists():
        os.environ["JAVA_HOME"] = JAVA_11

    # Databricks driver nodes run UTC. Matching that locally removes a real footgun:
    # collect() returns timestamps as naive datetimes in the DRIVER's zone, so on a
    # UTC+7 machine a stored 00:00Z arrives as 07:00 and any subsequent re-parse is
    # silently wrong. Prefer formatting inside Spark regardless; this makes the
    # fallback safe too.
    os.environ["TZ"] = "UTC"
    time.tzset()

    builder = (
        SparkSession.builder.appName(app)
        .master(f"local[{cores}]")
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog",
                "org.apache.spark.sql.delta.catalog.DeltaCatalog")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.shuffle.partitions", "8")
        .config("spark.ui.enabled", "false")
        # The dataset is under 1M rows, so the default 200 shuffle partitions and
        # adaptive coalescing only add overhead.
        .config("spark.sql.adaptive.enabled", "true")
    )
    spark = configure_spark_with_delta_pip(builder).getOrCreate()
    spark.sparkContext.setLogLevel("ERROR")
    return spark
