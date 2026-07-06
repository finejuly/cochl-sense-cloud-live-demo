export interface RecordingMetadata {
  duration_sec: number | null;
  content_type: string;
}

export interface SoundEvent {
  start_time_sec: number;
  end_time_sec: number;
  label: string;
  confidence: number | null;
}

export interface SpeechSegment {
  start_time_sec: number;
  end_time_sec: number;
  speaker: string | null;
  speaker_name: string | null;
  transcript: string;
}

export interface AudioInsights {
  contains_speech: boolean | null;
  detected_language: string | null;
  primary_sound_environment: string | null;
  situation_summary: string | null;
  notable_events: string[];
  keywords: string[];
}

export interface UsageMetadata {
  audio_duration_sec: number | null;
  services_used: string[];
  processing_time_ms: number;
}

export interface AnalysisResponse {
  recording: RecordingMetadata;
  sound_events: SoundEvent[];
  speech_segments: SpeechSegment[];
  audio_insights: AudioInsights | null;
  usage: UsageMetadata;
}

export type LiveChunkCollectionStatus = 'collected' | 'discarded_silent' | 'discarded_speech';

export interface LiveChunkAnalysisResponse {
  sequence_id: number;
  window_start_sec: number;
  window_end_sec: number;
  sound_events: SoundEvent[];
  processing_time_ms: number;
  collection_status?: LiveChunkCollectionStatus | null;
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
}

export interface LiveSessionEndResponse {
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

export interface CollectedSessionInfo {
  session_id: string;
  session_name: string | null;
  started_at: string | null;
  ended_at: string | null;
  segment_count: number;
  total_collected_duration_sec: number;
  segments: CollectedSegmentSummary[];
}

export interface CollectedSessionsResponse {
  sessions: CollectedSessionInfo[];
}
