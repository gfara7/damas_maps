#!/usr/bin/env python3
"""Direct Overture fetcher using DuckDB with corrected bbox filtering."""
import duckdb
import pandas as pd
import sys

def fetch_overture_places(bbox, release='2025-12-17.0', output_csv='overture_damascus.csv'):
    """Fetch places from Overture using DuckDB with bbox filtering."""
    south, west, north, east = bbox

    sql = f"""
    INSTALL spatial;
    LOAD spatial;
    INSTALL httpfs;
    LOAD httpfs;
    SET s3_region='us-west-2';

    SELECT
      id,
      names.primary AS name,
      categories.primary AS category,
      ST_Y(geometry) AS lat,
      ST_X(geometry) AS lon,
      CASE WHEN len(addresses) > 0 THEN addresses[1].freeform ELSE NULL END AS addr_street,
      CASE WHEN len(addresses) > 0 THEN addresses[1].locality ELSE NULL END AS addr_city,
      CASE WHEN len(websites) > 0 THEN websites[1] ELSE NULL END AS website,
      CASE WHEN len(phones) > 0 THEN phones[1] ELSE NULL END AS phone,
      'overture' AS source
    FROM read_parquet('s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*', filename=true, hive_partitioning=1)
    WHERE bbox.xmin <= {east} AND bbox.xmax >= {west}
      AND bbox.ymin <= {north} AND bbox.ymax >= {south};
    """

    print(f"Fetching Overture places from release {release}...")
    con = duckdb.connect()
    df = con.execute(sql).df()
    con.close()

    print(f"Found {len(df)} places from Overture")
    df.to_csv(output_csv, index=False, encoding='utf-8-sig')
    print(f"Saved to {output_csv}")
    return df

if __name__ == '__main__':
    bbox = (33.35, 36.15, 33.65, 36.45)  # Damascus
    fetch_overture_places(bbox)
