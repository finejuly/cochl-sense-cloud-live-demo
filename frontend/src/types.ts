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

export interface LiveChunkAnalysisResponse {
  sequence_id: number;
  window_start_sec: number;
  window_end_sec: number;
  sound_events: SoundEvent[];
  processing_time_ms: number;
}
