export interface SoundEvent {
  start_time_sec: number;
  end_time_sec: number;
  label: string;
  confidence: number | null;
}

export type LiveChunkCollectionStatus =
  | 'collected'
  | 'discarded_silent'
  | 'discarded_speech'
  | 'discarded_late';

export interface LiveCurationProgress {
  candidate_segment_count: number;
  selected_segment_count: number;
  rejected_repetitive_count: number;
  rejected_class_balance_count: number;
  rejected_session_budget_count: number;
  invalid_audio_count: number;
  write_error_count: number;
}

export interface LiveChunkAnalysisResponse {
  sequence_id: number;
  window_start_sec: number;
  window_end_sec: number;
  sound_events: SoundEvent[];
  processing_time_ms: number;
  collection_status?: LiveChunkCollectionStatus | null;
  curation_progress?: LiveCurationProgress | null;
}

export interface CollectedSegmentSummary {
  segment_index: number;
  start_sec: number;
  end_sec: number;
  duration_sec: number;
  event_count: number;
  labels: string[];
  audio_filename: string;
  metadata_filename: string;
  primary_label?: string | null;
  quota_label?: string | null;
  selection_reason?: string | null;
}

export interface CurationAggregates {
  candidate_segment_count: number;
  policy_selected_segment_count: number;
  policy_selected_duration_sec: number;
  policy_selected_audio_bytes: number;
  rejected_repetitive_count: number;
  rejected_class_balance_count: number;
  rejected_session_budget_count: number;
  invalid_audio_count: number;
  write_error_count: number;
  selected_label_segment_counts: Record<string, number>;
  selected_quota_duration_sec: Record<string, number>;
  policy_version?: number | null;
}

export interface LiveSessionEndResponse extends CurationAggregates {
  session_id: string;
  session_name?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  segment_count: number;
  total_collected_duration_sec: number;
  kept_chunk_count: number;
  discarded_silent_chunk_count: number;
  discarded_speech_chunk_count: number;
  segments: CollectedSegmentSummary[];
}

export interface CollectedSessionInfo extends CurationAggregates {
  session_id: string;
  session_name: string | null;
  started_at: string | null;
  ended_at: string | null;
  segment_count: number;
  total_collected_duration_sec: number;
  segments: CollectedSegmentSummary[];
  gcs_upload: GcsUploadStatus | null;
}

export interface GcsUploadStatus {
  status: 'uploaded';
  object_prefix: string;
  snapshot_id: string;
  uploaded_at: string;
}

export interface CollectedSessionsResponse {
  sessions: CollectedSessionInfo[];
}

export interface GcsSessionUploadResponse {
  status: 'uploaded';
  session_id: string;
  object_prefix: string;
  snapshot_id: string;
  uploaded_file_count: number;
  existing_file_count: number;
  total_size_bytes: number;
}

export interface GcsUploadFileProgress {
  object_name: string;
  source_filename: string;
  file_status: 'uploaded' | 'existing';
  completed_file_count: number;
  total_file_count: number;
}

export interface RuntimeCapabilities {
  gcs: boolean;
}

export interface RuntimeConfig {
  collection_confidence_threshold: number;
  api_token: string;
  capabilities: RuntimeCapabilities;
}
