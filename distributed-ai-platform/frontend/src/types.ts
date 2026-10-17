// ---------------------------------------------------------------------------
// Shared UI types. ssh-executor shapes live in api/ssh.ts (mirrors
// services/ssh-executor/schemas.py); storage-service shapes live here
// (mirrors services/storage-service/schemas.py).
// ---------------------------------------------------------------------------

export type JobStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled';

// --- Storage Service -------------------------------------------------------
export interface Dataset {
  id: string;
  name: string;
  description?: string | null;
  /** Same value as `storage_path`; kept for older servers. */
  hdfs_path: string;
  /** `local`, or `hdfs` for datasets stored in HDFS. */
  storage_backend?: string;
  /** The file's key in its store. */
  storage_path?: string;
  file_format: string;
  row_count?: number | null;
  column_count?: number | null;
  size_bytes?: number | null;
  /**
   * JSON string. Image datasets record
   * `{columns, feature_columns, target_column, num_classes, class_names, image: {side, channels}}`.
   * Tabular uploads may leave it empty.
   */
  schema_json?: string | null;
  validation_status: string;
  validation_message?: string | null;
  created_at: string;
  updated_at?: string;
  created_by?: string | null;
}

/** Parsed form of Dataset.schema_json (all fields optional). */
export interface DatasetSchema {
  columns?: string[];
  feature_columns?: string[];
  target_column?: string;
  num_classes?: number;
  class_names?: string[];
  image?: { side: number; channels: number };
}
