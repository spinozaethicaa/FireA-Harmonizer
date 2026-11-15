export type BBox = [number, number, number, number];
export type Mode = "nrt" | "sp" | "combined";

export interface QualityParams {
  modis_min_confidence: number;
  viirs_classes: string[];
  daynight: "all" | "D" | "N";
}

export interface MatchParams {
  cross_dist_km: number;
  cross_dt_min: number;
  use_pixel_size: boolean;
  same_pass_adj_km: number;
  pass_window_min: number;
  max_cluster_span_min: number;
  max_cluster_extent_km: number;
  cont_dist_km: number;
  cont_gap_h: number;
  max_event_extent_km: number;
}

export interface ApiErrorInfo {
  kind: string;
  message: string;
  status?: number | null;
}

export interface ProductAvailability {
  data_id: string;
  min_date: string;
  max_date: string;
}

export interface DatasetMeta {
  dataset_id: string;
  kind: "api" | "upload" | "sample";
  is_sample: boolean;
  label: string;
  bbox: BBox | null;
  start: string;
  end: string;
  sources: Record<string, string>;
  segments: { instrument: string; source: string; start: string; end: string }[];
  n_observations: number;
  chunk_stats: Record<string, number>;
  n_chunks: number;
  n_from_cache: number;
  errors: { source: string; start: string; day_range: number; error: ApiErrorInfo | null }[];
  n_errors: number;
  duplicates_removed: number;
  warnings: string[];
  products_versions: Record<string, string[]>;
}

export interface Obs {
  observation_id: string;
  source: string;
  satellite: string;
  instrument: "MODIS" | "VIIRS";
  latitude: number;
  longitude: number;
  observed_at_utc: string;
  frp_mw: number | null;
  confidence_raw: string;
  quality_pass: boolean;
  event_id: string | null;
  cross_matched: boolean;
  n_cross_links: number;
}

export interface ObsFull extends Obs {
  scan_km: number | null;
  track_km: number | null;
  daynight: string | null;
  version: string;
  origin: string;
  raw: Record<string, string>;
  quality_excluded_reason: string | null;
  pass_id: string | null;
  cluster_id: string | null;
  match_flags: string;
}

export interface CrossLink {
  link_id: string;
  modis_observation_id: string;
  viirs_observation_id: string;
  modis_satellite: string;
  viirs_satellite: string;
  distance_km: number;
  threshold_km: number;
  threshold_basis: string;
  dt_min: number;
  same_cluster: boolean;
}

export interface ContinuityLink {
  from_cluster_id: string;
  to_cluster_id: string;
  event_id: string;
  to_event_id: string;
  distance_km: number;
  gap_h: number;
  status: "kabul" | "belirsiz";
  reason: string;
}

export interface PassSummary {
  pass_id: string;
  satellite: string;
  instrument: string;
  time_utc: string;
  n_obs: number;
  frp_max_mw: number | null;
  frp_pass_sum_mw: number | null;
  n_frp_missing: number;
}

export interface EventLite {
  event_id: string;
  first_obs_utc: string;
  last_obs_utc: string;
  days: string[];
  n_days: number;
  n_obs: number;
  n_modis: number;
  n_viirs: number;
  n_cross_matched_obs: number;
  n_cross_links: number;
  n_clusters: number;
  centroid_lat: number;
  centroid_lon: number;
  bbox: BBox;
  extent_km: number;
  max_single_frp_mw: number | null;
  n_flags: number;
}

export interface EventFull extends EventLite {
  satellites: string[];
  sources: string[];
  n_unmatched_obs: number;
  passes: PassSummary[];
  flags: string[];
  ambiguous_links: ContinuityLink[];
  explanation: string;
}

export type DayClass = "both" | "modis_only" | "viirs_only" | "none" | "unavailable";

export interface CalendarDay {
  date: string;
  sensors: Record<"MODIS" | "VIIRS", { coverage: string; product: string | null; n_detections: number; n_excluded_quality: number }>;
  class: DayClass;
  partial_coverage: boolean;
  n_events: number;
  n_cross_links: number;
  n_events_with_cross_match: number;
}

export interface QualitySummary {
  [instr: string]: { total: number; kept: number; excluded: number; excluded_by_reason: Record<string, number> };
}

export interface Summary {
  analysis_key: string;
  dataset: DatasetMeta;
  quality_params: QualityParams;
  match_params: MatchParams;
  quality_summary: QualitySummary;
  events: EventLite[];
  calendar: { timezone: string; days: CalendarDay[] };
  stats: {
    n_observations_total: number;
    n_observations_used: number;
    n_events: number;
    n_cross_links: number;
    n_ambiguous_continuity: number;
    n_multi_modis_viirs: number;
    map_point_limit: number;
  };
  time_note: string;
  virtual_modis?: VirtualModis;
}

export interface JobStatus {
  job_id: string;
  status: "running" | "done" | "error" | "cancelled";
  elapsed_s?: number;
  phase: string;
  done: number;
  total: number;
  rows: number;
  message: string;
  result: Summary | null;
  error: ApiErrorInfo | null;
}

export interface VMMetrics {
  poisson_deviance: number | null;
  mae: number | null;
  auc_detection: number | null;
  interval80_coverage: number | null;
  region_day_total_abs_pct_err_median: number | null;
}

export interface VMModel {
  name: string;
  n_params: number;
  trained_at_utc: string;
  target: string;
  test_regions: string[];
  train_regions: string[];
  test_metrics: { FireA: VMMetrics | null; gbm: VMMetrics | null; power_law: VMMetrics | null };
  split_sizes: { train: number; val: number; test: number } | null;
}

export interface VMDaily {
  date: string;
  aqua_observed: number;
  viirs_pixels: number;
  viirs_clusters?: number;
  virtual_mean: number;
  virtual_lo: number;
  virtual_hi: number;
}

export interface VirtualModis {
  available: boolean;
  reason?: string;
  model?: VMModel;
  daily?: VMDaily[];
  n_clusters?: number;
  totals?: { viirs_pixels: number; virtual_mean: number; aqua_observed: number };
  note?: string;
}

export interface VMCluster {
  cluster: number;
  lat: number;
  lon: number;
  time_utc: string;
  date: string;
  satellite: string;
  n_viirs: number;
  frp_sum_mw: number;
  modis_like_mean: number;
  modis_like_lo: number;
  modis_like_hi: number;
  p_detect: number;
}

export interface ClimMonth {
  ym: string;
  year: number;
  month: number;
  partial: boolean;
  modis_cover: number;
  viirs_cover: number;
  aqua: number | null;
  aqua_frp: number | null;
  modis_all: number | null;
  viirs: number | null;
  viirs_frp: number | null;
  harm: number | null;
  harm_raw: number | null;
  harm_fire?: number | null;
  harm_lo: number | null;
  harm_hi: number | null;
  harm_frp: number | null;
  harm_raw_frp: number | null;
  harm_frp_lo: number | null;
  harm_frp_hi: number | null;
  harm_sats?: string[];
  aqua_drift_min?: number | null;
  combined: number | null;
  combined_frp: number | null;
  combined_source: string | null;
  products: string[];
  [field: string]: unknown;
}

export interface ClimSeason {
  month: number;
  name: string;
  median: number | null;
  q25: number | null;
  q75: number | null;
  n_years: number;
  share: number;
  critical: boolean;
}

export interface ClimRecent {
  start: string;
  end: string;
  value: number;
  source: string | null;
  baseline_median: number | null;
  robust_spread: number | null;
  robust_z: number | null;
  n_years: number;
  p_high: number | null;
  p_low: number | null;
}

export interface ClimAnnual {
  year: number;
  partial: boolean;
  [field: string]: number | boolean | null;
}

export interface ClimGrid {
  west: number;
  south: number;
  size: number;
  nx: number;
  ny: number;
  cell_min_spread: { count: number; frp: number };
  count: Record<string, [number, number][]>;
  frp: Record<string, [number, number][]>;
  source: Record<string, "a" | "h">;
}

export interface ClimResult {
  bbox: number[];
  first_year: number;
  viirs_start_year: number;
  last_day: string | null;
  months: ClimMonth[];
  annual: ClimAnnual[];
  season: ClimSeason[];
  critical_months: number[];
  critical_share: number;
  season_frp: ClimSeason[];
  critical_months_frp: number[];
  critical_share_frp: number;
  recent: ClimRecent | null;
  ratio_viirs_to_aqua_median: number | null;
  ratio_harmonized_to_aqua_median: number | null;
  n_overlap_months: number;
  anomaly_floor: { count: number; frp: number };
  calibration: ClimCalibration;
  calibration_frp: ClimCalibration;
  grid: ClimGrid;
  model?: string | null;
  model_choice?: { chosen: string | null; criterion: string; candidates: { name: string | null; harm_mae: number | null;
    harm_month_err_pct: number | null; harm_year_err_pct: number | null; raw_mae: number | null; raw_month_err_pct: number | null }[] };
}

export interface ClimValidation {
  n_months: number;
  n_years: number;
  n_active_months: number;
  method: string;
  const_kind: "global_ratio" | "local_ratio";
  years: number[];
  harm_month_err_pct: number | null;
  raw_month_err_pct: number | null;
  const_month_err_pct: number | null;
  harm_year_err_pct: number | null;
  raw_year_err_pct: number | null;
  const_year_err_pct: number | null;
  harm_mae: number | null;
  raw_mae: number | null;
  const_mae: number | null;
  coverage80_raw: number | null;
  coverage80_calibrated: number;
  fire_a_bias_pct: number | null;
  harm_year_err_mean_pct?: number | null;
  season_month_err_pct?: number | null;
  season_year_err_pct?: number | null;
  season_year_err_mean_pct?: number | null;
  season_mae?: number | null;
  multi_sat?: { n_months: number; years: number[]; n_active_months: number; harm_month_err_pct: number | null;
    harm_year_err_pct: number | null; harm_year_err_mean_pct: number | null; harm_mae: number | null } | null;
}

export interface ClimCalibration {
  season_factor: number[];
  spread: number[];
  unit: number;
  method: "seasonal" | "seam" | "none" | "uncalibrated";
  validation: ClimValidation | null;
  aqua_drift_min?: Record<string, number>;
  drifted_years?: number[];
  viirs_sats?: string[];
  xcal_noaa21?: number | null;
  size_correction?: { applied: boolean; n_labels: number; sizes: string[]; day: number[]; night: number[];
    check?: { mae_with: number; mae_without: number } | null };
}
