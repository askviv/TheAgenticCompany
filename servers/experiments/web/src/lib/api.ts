export type Experiment = {
  id: string
  task_id?: string
  provider_id?: string
  classification?: "financial" | "operational"
  source_revision?: string
  requested_outcome?: string
  role_fit?: string
  role_fit_note?: string
  financial_context_status?: string
  title: string
  summary: string
  role: string
  dependencies: string[]
  description: string
  kind?: "workplace" | "simulation"
  benchmarks?: string[]
  display_description: string
}

export type CompatibilityCatalogue = {
  schema_version: number
  company_persona: string
  library_inventory: { name: string; count: number; classification: string; summary: string }[]
  tau_domains: {
    name: string
    catalogue_count: number
    default_base_count: number
    fit: string
    summary: string
    implementation: string
    source_integrity_issue_count?: number
    financial_task_count?: number
    remaining_carrier_state_task_count?: number
    refuel_data_task_count?: number
  }[]
  source_revisions: { name: string; revision: string }[]
}

export type ExperimentRunStatus =
  | "initializing"
  | "running"
  | "paused"
  | "queued"
  | "sending"
  | "awaiting_viv"
  | "viv_replied"
  | "failed"
  | "uncertain"
  | "completed"

export type RunMessage = {
  author: string
  text: string
  timestamp: string
}

export type ExperimentRun = {
  id: string
  experiment_id: string
  title: string
  status: ExperimentRunStatus
  created_at: string
  error: string | null
  messages: RunMessage[]
  chat_url: string | null
  kind?: "workplace" | "simulation"
  simulation_run_id?: string | null
  provider_url?: string | null
  provider_status?: string | null
  simulated_time?: string | null
  activity?: { id: string; timestamp: string; event: string; description: string }[]
  revision: number
}

export type EvaluationStatus = "completed" | "incomplete" | "failed"
export type EvaluationMode = "native_benchmark" | "company_adapted"
export type EvaluationCriterion = {
  criterion_id?: string
  criterion?: string
  name?: string
  status?: string
  score?: number | string | boolean | null
  value?: number | string | boolean | null
  actual?: number | string | boolean | null
  expected?: number | string | boolean | null
  maximum?: number | string | boolean | null
  max_score?: number | string | boolean | null
  weight?: number | string | boolean | null
  gap_code?: string
  passed?: boolean
}
export type Evaluation = {
  evaluation_id: string
  company_id?: string
  experiment_id?: string
  run_id: string
  generation?: number
  benchmark?: string | null
  domain?: string | null
  benchmark_version?: string | null
  evaluation_mode?: EvaluationMode | null
  evaluated_at?: string
  requested_at?: string
  evidence_ref?: string
  evidence_hash?: string
  status: EvaluationStatus
  scores?: Record<string, number | string | boolean | null>
  criterion_results?: EvaluationCriterion[]
  warnings?: string[]
  mapping_gaps?: string[]
  native_scale?: Record<string, "boolean" | "categorical" | "seconds" | "native_reward_0_to_1" | "count" | "native_metric">
  correlation_id?: string
  failure?: { code?: string; exception_type?: string } | null
  lifecycle?: "reserved" | "running" | "uncertain" | string
}

export type CompanyNativeEvaluation = Omit<Evaluation, "run_id"> & {
  provider_id: "coffeebench" | "ecommercebench"
  source_id?: string
  upstream_commit?: string
  initialization_request_hash?: string
  seed?: number
  started_at?: string
  native_state_ref?: string
  native_state_hash?: string
  native_company_time?: string
  scope: "company_global"
  attribution: "company_global_not_experiment"
  money_metric_units?: Record<string, string>
}

export type CompanyNativeEvaluationProvider = {
  provider_id: CompanyNativeEvaluation["provider_id"]
  evaluations: CompanyNativeEvaluation[]
  latest: CompanyNativeEvaluation | null
  selection: "latest_saved"
  error?: { code: string; correlation_id?: string }
}

export type Session = { csrf_token: string; idempotency_key: string }
type RunResponse = { run_id: string; url: string }

export type Company = {
  background?: {
    initialized: boolean
    providers: Record<string, { status: "active" | "error" | "stopped" | "stopping" | "starting"; enabled?: boolean; in_flight?: boolean; initialized: boolean; next_due?: string | null }>
    errors: Record<string, { code: string }>
  } | null
  id: string
  name: string
  status: "running" | "paused" | "stopped" | "starting" | string
  started_at?: string | null
  updated_at?: string | null
  simulated_time?: string | null
  error?: string | null
}

export type CompanyDesktopEmployee = {
  id: string
  name: string
  role: string
  status: string
  viewing_url?: string | null
}

export type CompanyDesktopSession = {
  employee_id: string
  status: string
  viewing_url: string
}

export type ActivityEntry = {
  id: string
  type: "email" | "message" | "action" | "company" | string
  timestamp: string
  title: string
  summary?: string
  content?: string
  actor?: string | null
  direction?: "sent" | "received" | null
  run_id?: string | null
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(url, { credentials: "same-origin", ...init })
  } catch (cause) {
    throw new Error("The server could not be reached. Check your connection and try again.", { cause })
  }

  if (!response.ok) {
    let code: string | undefined
    try {
      const payload = await response.clone().json() as { error?: { code?: unknown } }
      if (typeof payload.error?.code === "string" && /^[A-Za-z][A-Za-z0-9_.-]{0,95}$/.test(payload.error.code)) {
        code = payload.error.code
      }
    } catch {
      // Some existing endpoints return plain text or an empty error body.
    }
    throw new Error(`Request failed (${response.status}${code ? `: ${code}` : ""}). Try again or return to the run tracker.`)
  }
  return (await response.json()) as T
}

export async function getCatalog(): Promise<{
  experiments: Experiment[]
  provider_catalog_error?: { code: string; correlation_id?: string } | null
}> {
  return requestJson("/api/catalog")
}

export async function getCompatibilityCatalogue(): Promise<CompatibilityCatalogue> {
  return requestJson<CompatibilityCatalogue>("/api/compatibility")
}

export async function getSession(): Promise<Session> {
  return requestJson<Session>("/api/session")
}

export async function getRuns(): Promise<ExperimentRun[]> {
  const result = await requestJson<{ runs: ExperimentRun[] }>("/api/runs")
  return result.runs
}

export async function getCompany(): Promise<Company> {
  const result = await requestJson<{ company: Company }>("/api/company")
  return result.company
}

export async function getCompanyDesktops(): Promise<CompanyDesktopEmployee[]> {
  const result = await requestJson<{ employees: CompanyDesktopEmployee[] }>('/api/company/desktops')
  return result.employees
}

export async function startCompanyDesktop(employeeId: string): Promise<CompanyDesktopSession> {
  const session = await getSession()
  const result = await requestJson<CompanyDesktopSession>('/api/company/desktops/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      employee_id: employeeId,
      csrf_token: session.csrf_token,
      idempotency_key: session.idempotency_key,
    }),
  })
  return { employee_id: result.employee_id, status: result.status, viewing_url: result.viewing_url }
}

export async function stopCompanyDesktop(employeeId: string): Promise<{ employee_id: string; status: string }> {
  const session = await getSession()
  return requestJson('/api/company/desktops/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      employee_id: employeeId,
      csrf_token: session.csrf_token,
      idempotency_key: session.idempotency_key,
    }),
  })
}

export async function setBackgroundProviderEnabled(providerId: CompanyNativeEvaluation["provider_id"], enabled: boolean): Promise<{ provider_id: CompanyNativeEvaluation["provider_id"]; enabled: boolean; in_flight: boolean; status: "stopped" | "stopping" | "starting" | "active" | "error" }> {
  const session = await getSession()
  return requestJson(`/api/company/background/${providerId}/control`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
      "Idempotency-Key": session.idempotency_key,
    },
    body: new URLSearchParams({ csrf_token: session.csrf_token, enabled: String(enabled) }),
  })
}

export async function getCompanyEvaluations(): Promise<Record<CompanyNativeEvaluation["provider_id"], CompanyNativeEvaluationProvider>> {
  const result = await requestJson<{ providers: Record<CompanyNativeEvaluation["provider_id"], CompanyNativeEvaluationProvider> }>("/api/company/evaluations")
  return result.providers
}

export async function startCompanyEvaluation(providerId: CompanyNativeEvaluation["provider_id"], idempotencyKey: string): Promise<CompanyNativeEvaluation> {
  const session = await getSession()
  const result = await requestJson<{ evaluation: CompanyNativeEvaluation }>(`/api/company/evaluations/${encodeURIComponent(providerId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
    body: new URLSearchParams({ csrf_token: session.csrf_token, idempotency_key: idempotencyKey }),
  })
  return result.evaluation
}

export type ActivityMetrics = { emails_sent: number | null; emails_received: number | null; messages: number; open_experiments: number | null }

export async function getActivity(): Promise<{ activity: ActivityEntry[]; error?: string | null; metrics: ActivityMetrics }> {
  return requestJson<{ activity: ActivityEntry[]; error?: string | null; metrics: ActivityMetrics }>("/api/activity")
}

export async function setCompanyRunning(running: boolean): Promise<Company> {
  const session = await getSession()
  const result = await requestJson<{ company: Company }>(running ? "/company/start" : "/company/pause", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    },
    body: new URLSearchParams({
      csrf_token: session.csrf_token,
      idempotency_key: session.idempotency_key,
      ...(!running ? { paused: "true" } : {}),
    }),
  })
  return result.company
}

export async function getRun(runId: string): Promise<ExperimentRun> {
  const result = await requestJson<{ run: ExperimentRun }>(`/api/runs/${encodeURIComponent(runId)}`)
  return result.run
}

export async function getEvaluations(runId: string): Promise<Evaluation[]> {
  const result = await requestJson<{ evaluations: Evaluation[] }>(
    `/api/runs/${encodeURIComponent(runId)}/evaluations`,
  )
  return result.evaluations
}

export async function getLatestEvaluation(runId: string): Promise<Evaluation | null> {
  const result = await requestJson<{ evaluation: Evaluation | null }>(
    `/api/runs/${encodeURIComponent(runId)}/evaluations/latest`,
  )
  return result.evaluation
}

export async function getEvaluation(runId: string, evaluationId: string): Promise<Evaluation> {
  const result = await requestJson<{ evaluation: Evaluation }>(
    `/api/runs/${encodeURIComponent(runId)}/evaluations/${encodeURIComponent(evaluationId)}`,
  )
  return result.evaluation
}

async function postForm(url: string, values: Record<string, string>): Promise<RunResponse> {
  return requestJson<RunResponse>(url, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    },
    body: new URLSearchParams(values),
  })
}

export async function startRun(experimentId: string, session: Session): Promise<RunResponse> {
  return postForm(`/experiments/${encodeURIComponent(experimentId)}/runs`, {
    csrf_token: session.csrf_token,
    idempotency_key: session.idempotency_key,
  })
}

export async function refreshRun(runId: string): Promise<RunResponse> {
  const session = await getSession()
  return postForm(`/runs/${encodeURIComponent(runId)}/refresh`, {
    csrf_token: session.csrf_token,
  })
}

export async function startEvaluation(runId: string, session: Session): Promise<Evaluation> {
  const result = await requestJson<{ evaluation: Evaluation }>(`/runs/${encodeURIComponent(runId)}/evaluate`, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    },
    body: new URLSearchParams({
      csrf_token: session.csrf_token,
      idempotency_key: session.idempotency_key,
    }),
  })
  return result.evaluation
}

export async function retryRun(runId: string): Promise<RunResponse> {
  const session = await getSession()
  return postForm(`/runs/${encodeURIComponent(runId)}/retry`, {
    csrf_token: session.csrf_token,
  })
}
