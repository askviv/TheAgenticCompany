import { useEffect, useMemo, useRef, useState } from "react"
import { CompanyPackImport } from "@/CompanyPackImport"
import { CompanySwitcher } from "@/CompanySwitcher"
import { Switch } from "@base-ui/react/switch"
import {
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  Beaker,
  Building2,
  CircleAlert,
  Clock3,
  ExternalLink,
  LoaderCircle,
  Mail,
  Maximize2,
  MessageCircle,
  RefreshCw,
  Search,
} from "lucide-react"
import {
  BrowserRouter,
  Link,
  NavLink,
  Outlet,
  Route,
  Routes,
  useNavigate,
  useOutletContext,
  useParams,
  useSearchParams,
} from "react-router"
import {
  getActivity,
  resendActivityEmail,
  getBenchmarkStatus,
  type BenchmarkStatus,
  getModelControl,
  setModelControl,
  getCatalog,
  getCompany,
  getCompanyClock,
  setCompanyClockSpeed,
  type CompanyClock,
  getCoffeePaymentWindows,
  getEcommerceBehaviours,
  setEcommerceBehaviour,
  getCompanyDesktops,
  getCompanyEvaluations,
  getCompanyTools,
  getCodexAuthStatus,
  getCodexDeviceAuthorization,
  getEvaluation,
  getEvaluations,
  getLatestEvaluation,
  getRun,
  getRuns,
  getSession,
  refreshRun,
  retryRun,
  startRun,
  startEvaluation,
  startCompanyEvaluation,
  setBackgroundProviderEnabled,
  setCoffeePaymentWindow,
  startCodexDeviceAuthorization,
  type ActivityEntry,
  type ActivityMetrics,
  type CoffeePaymentWindows,
  type Company,
  type CompanyDesktopEmployee,
  type CompanyNativeEvaluation,
  type CompanyNativeEvaluationProvider,
  type CodexAuthStatus,
  type CompanyToolLink,
  type Experiment,
  type ExperimentRun,
  type ExperimentRunStatus,
  type Evaluation,
  type Session,
} from "@/lib/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

type LayoutContext = { refreshKey: number; companyClock: CompanyClock | null }

const statusText: Record<ExperimentRunStatus, string> = {
  initializing: "Preparing company",
  running: "Running",
  paused: "Paused",
  queued: "Queued",
  sending: "Sending message",
  awaiting_viv: "Waiting for reply",
  viv_replied: "Reply received",
  failed: "Failed",
  uncertain: "Needs a check",
  completed: "Completed",
}

const statusColor: Record<ExperimentRunStatus, string> = {
  initializing: "border-blue-200 bg-blue-50 text-blue-800",
  running: "border-blue-200 bg-blue-50 text-blue-800",
  paused: "border-amber-300 bg-amber-50 text-amber-800",
  queued: "border-amber-300 bg-amber-50 text-amber-800",
  sending: "border-blue-200 bg-blue-50 text-blue-800",
  awaiting_viv: "border-blue-200 bg-blue-50 text-blue-800",
  viv_replied: "border-emerald-200 bg-emerald-50 text-emerald-800",
  failed: "border-red-200 bg-red-50 text-red-800",
  uncertain: "border-orange-200 bg-orange-50 text-orange-800",
  completed: "border-slate-200 bg-slate-100 text-slate-700",
}

function statusLabel(status: string): string {
  return statusText[status as ExperimentRunStatus] ?? status.replaceAll("_", " ")
}

function StatusBadge({ status }: { status: string }) {
  const color = statusColor[status as ExperimentRunStatus] ?? "border-border bg-muted text-muted-foreground"
  const isActive = status === "sending" || status === "awaiting_viv"
  return (
    <Badge variant="outline" className={`gap-1.5 rounded-full px-2.5 py-1 font-medium ${color}`}>
      <span className={`size-1.5 rounded-full bg-current ${isActive ? "animate-pulse" : ""}`} aria-hidden="true" />
      {statusLabel(status)}
    </Badge>
  )
}

function ErrorNotice({ message, title = "Something went wrong" }: { message: string; title?: string }) {
  return (
    <Alert variant="destructive">
      <CircleAlert />
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription>{message}</AlertDescription>
    </Alert>
  )
}

function LoadingCards() {
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" aria-label="Loading experiments">
      {Array.from({ length: 6 }, (_, index) => (
        <Card key={index} className="min-h-52">
          <CardHeader className="gap-4">
            <Skeleton className="h-5 w-24" />
            <Skeleton className="h-6 w-3/4" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
          </CardHeader>
        </Card>
      ))}
    </div>
  )
}

function ModelCallControl() {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    getModelControl().then((state) => setEnabled(state.enabled)).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : "Model call status unavailable")
    })
  }, [])
  const change = async () => {
    setBusy(true)
    setError(null)
    try {
      const state = await setModelControl(enabled === false)
      setEnabled(state.enabled)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Model call control failed")
    } finally { setBusy(false) }
  }
  return <div className="flex items-center gap-2" aria-live="polite">
    <span className="text-sm">{enabled === false ? "Model calls stopped" : enabled === true ? "Model calls enabled" : "Model call status unavailable"}</span>
    <Button variant={enabled === false ? "outline" : "destructive"} disabled={busy} onClick={() => void change()}>
      {busy ? "Updating…" : enabled === false ? "Resume model calls" : "Stop all model calls"}
    </Button>
    {error && <span role="alert" className="text-sm text-destructive">{error}</span>}
  </div>
}

function AppLayout() {
  const [refreshKey, setRefreshKey] = useState(0)
  const [companyClock, setCompanyClock] = useState<CompanyClock | null>(null)
  const snapshot = useRef<Map<string, string> | undefined>(undefined)

  useEffect(() => {
    let active = true
    let pending = false
    const refreshClock = () => {
      if (pending) return
      pending = true
      getCompanyClock().then((clock) => {
        if (active) setCompanyClock(clock)
      }).catch((error) => {
        if (active) setCompanyClock(null)
        console.error("Unable to load company time.", error instanceof Error ? error.name : "UnknownError")
      }).finally(() => { pending = false })
    }
    refreshClock()
    const interval = window.setInterval(refreshClock, 1000)
    return () => { active = false; window.clearInterval(interval) }
  }, [])

  useEffect(() => {
    const source = new EventSource("/api/events")
    source.onerror = () => console.warn("Live updates are reconnecting.")
    source.addEventListener("runs", (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as {
          runs?: Array<{ id: string; status: string; revision: number }>
        }
        const next = new Map((payload.runs ?? []).map((run) => [run.id, `${run.status}:${run.revision}`]))
        const previous = snapshot.current
        const changed =
          previous === undefined
            ? next.size > 0
            : next.size !== previous.size || [...next].some(([id, value]) => previous.get(id) !== value)
        snapshot.current = next
        if (changed) setRefreshKey((current) => current + 1)
      } catch (reason) {
        console.error("Unable to read live run update.", reason instanceof Error ? reason.name : "UnknownError")
      }
    })
    source.addEventListener("activity", () => setRefreshKey((current) => current + 1))
    source.addEventListener("company", () => setRefreshKey((current) => current + 1))
    return () => source.close()
  }, [])

  return (
    <div className="min-h-svh bg-background text-foreground">
      <header className="sticky top-0 z-20 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80">
        <div className="flex min-h-14 w-full flex-wrap items-center justify-between px-4 sm:px-6">
          <Link to="/" className="flex items-center gap-2.5 font-semibold tracking-tight" aria-label="The Agentic Company activity">
            <span className="grid size-8 place-items-center rounded-lg bg-primary text-primary-foreground"><Beaker className="size-4" /></span>
            <span>The Agentic Company</span>
          </Link>
          <ModelCallControl />
          <Link to="/settings/company-clock" className="basis-full pb-2 text-center text-xs text-muted-foreground hover:underline lg:basis-auto lg:pb-0">
            Company time {companyClock?.simulated_time ? formatDate(companyClock.simulated_time) : "—"}
            {companyClock?.clock_speed != null && ` · ${companyClock.clock_speed}×`}
          </Link>
        </div>
      </header>
      <div className="md:grid md:grid-cols-[14rem_minmax(0,1fr)]">
        <nav className="border-b p-4 md:min-h-[calc(100svh-3.5rem)] md:border-r md:border-b-0" aria-label="Main navigation">
          <div className="flex flex-wrap gap-1 md:flex-col">
            {[
              ["/", "Activity"],
              ["/streams", "Observe"],
              ["/experiments", "Experiments"],
              ["/runs", "Runs"],
              ["/help/native-evaluations", "Native evaluations"],
              ["/help/company-tools", "Company tools"],
              ["/help", "Help"],
            ].map(([path, label]) => <SidebarLink key={path} path={path} label={label} />)}
          </div>
          <p className="mt-5 px-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Settings</p>
          <div className="mt-2 flex flex-wrap gap-1 md:flex-col">
            {[
              ["/help/company-environment", "Company environment"],
              ["/settings/company-clock", "Company clock"],
              ["/help/context", "Codex connection"],
              ["/experiments/background", "Background simulations"],
              ["/experiments/coffeebench/settings", "CoffeeBench behaviours"],
              ["/experiments/ecommercebench/settings", "E-CommerceBench behaviours"],
            ].map(([path, label]) => <SidebarLink key={path} path={path} label={label} />)}
          </div>
        </nav>
        <main className="min-w-0 px-4 py-8 sm:px-6 sm:py-10">
          <Outlet context={{ refreshKey, companyClock } satisfies LayoutContext} />
        </main>
      </div>
    </div>
  )
}

function CompanyClockPage() {
  const { companyClock } = useOutletContext<LayoutContext>()
  const [speed, setSpeed] = useState("")
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmed, setConfirmed] = useState<number | null>(null)
  const kernel = companyClock?.clock_mode === "kernel"
  const requested = Number(speed)
  const valid = Number.isFinite(requested) && requested > 0 && (!kernel || Number.isSafeInteger(requested))

  async function save() {
    if (saving || !companyClock?.can_set_speed || !valid) return
    setSaving(true)
    setError(null)
    setConfirmed(null)
    try {
      setConfirmed(await setCompanyClockSpeed(requested))
      setSpeed("")
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The clock speed could not be confirmed.")
    } finally {
      setSaving(false)
    }
  }

  const instant = companyClock?.simulated_time
  const displayTime = instant
    ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium", timeZone: "UTC" }).format(new Date(instant)) + " UTC"
    : "Unavailable"

  return <section className="max-w-xl space-y-5">
    <h1 className="text-2xl font-semibold tracking-tight">Company clock</h1>
    <Card>
      <CardContent className="space-y-5">
        <dl className="grid grid-cols-2 gap-4">
          <div><dt className="text-sm text-muted-foreground">Simulated time</dt><dd className="mt-1 font-medium">{displayTime}</dd></div>
          <div><dt className="text-sm text-muted-foreground">Current speed</dt><dd className="mt-1 font-medium">{companyClock?.clock_speed == null ? "Unavailable" : `${companyClock.clock_speed}×`}</dd></div>
        </dl>
        <form className="flex flex-wrap items-end gap-3" onSubmit={(event) => { event.preventDefault(); void save() }}>
          <div className="space-y-2">
            <label htmlFor="company-clock-speed" className="text-sm font-medium">New speed (×)</label>
            <Input id="company-clock-speed" type="number" min={kernel ? 1 : undefined}
              step={kernel ? 1 : "any"} value={speed}
              onChange={(event) => { setSpeed(event.target.value); setConfirmed(null) }}
              disabled={!companyClock?.can_set_speed || saving} />
          </div>
          <Button type="submit" disabled={!companyClock?.can_set_speed || !valid || saving}>
            {saving ? "Applying…" : "Apply speed"}
          </Button>
        </form>
        {companyClock && !companyClock.can_set_speed && <p className="text-sm text-muted-foreground">Speed control is unavailable.</p>}
        {confirmed !== null && <p role="status" className="text-sm">Clock speed updated to {confirmed}×.</p>}
        {error && <ErrorNotice title="Clock speed" message={error} />}
      </CardContent>
    </Card>
  </section>
}

function SidebarLink({ path, label }: { path: string, label: string }) {
  return (
    <NavLink to={path} end={!["/experiments", "/runs", "/help"].includes(path)} className={({ isActive }) =>
      `rounded-md px-3 py-2 text-sm transition-colors ${isActive
        ? "bg-secondary font-medium text-foreground"
        : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}>
      {label}
    </NavLink>
  )
}

function useRefreshKey() {
  return useOutletContext<LayoutContext>().refreshKey
}

function EmployeeStreamsPage() {
  const refreshKey = useRefreshKey()
  const [manualRefresh, setManualRefresh] = useState(0)
  const [employees, setEmployees] = useState<CompanyDesktopEmployee[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [focusedId, setFocusedId] = useState<string | null>(null)
  const [fullscreenError, setFullscreenError] = useState<string | null>(null)
  const focusedViewer = useRef<HTMLDivElement>(null)
  const desktopsRequest = useRef<Promise<CompanyDesktopEmployee[]> | null>(null)

  useEffect(() => {
    if (!focusedId) return
    let wasFullscreen = Boolean(document.fullscreenElement)
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault()
        setFocusedId(null)
      }
    }
    const onFullscreenChange = () => {
      if (wasFullscreen && !document.fullscreenElement) setFocusedId(null)
      wasFullscreen = Boolean(document.fullscreenElement)
    }
    window.addEventListener("keydown", closeOnEscape, true)
    document.addEventListener("fullscreenchange", onFullscreenChange)
    return () => {
      window.removeEventListener("keydown", closeOnEscape, true)
      document.removeEventListener("fullscreenchange", onFullscreenChange)
    }
  }, [focusedId])

  useEffect(() => {
    let active = true
    const pending = desktopsRequest.current ?? getCompanyDesktops()
    desktopsRequest.current = pending
    pending.then((items) => {
      if (!active) return
      setEmployees(items)
      setError(null)
    }).catch((reason) => {
      if (!active) return
      setError(reason instanceof Error ? reason.message : "Employee desktop streams could not be loaded.")
    }).finally(() => {
      if (desktopsRequest.current === pending) desktopsRequest.current = null
      if (!active) return
      setLoading(false)
    })
    return () => { active = false }
  }, [refreshKey, manualRefresh])

  const running = employees.filter((employee) =>
    ["running", "active", "ready"].includes(employee.status.toLowerCase()),
  )
  const focusedEmployee = running.find((employee) => employee.id === focusedId)

  const enterFullscreen = async () => {
    try {
      await focusedViewer.current?.requestFullscreen()
      setFullscreenError(null)
    } catch {
      setFullscreenError("Fullscreen mode is unavailable in this browser.")
    }
  }

  return (
    <section className="space-y-6">
      {!focusedEmployee && (
        <div className="flex justify-end">
          <Button variant="outline" size="sm" disabled={loading} onClick={() => {
            setLoading(true)
            setManualRefresh((current) => current + 1)
          }}><RefreshCw /> Refresh streams</Button>
        </div>
      )}
      {error && <ErrorNotice message={error} title="Employee streams unavailable" />}
      {loading && <p className="text-sm text-muted-foreground">Loading running desktops…</p>}
      {!loading && !error && running.length === 0 && (
        <Card><CardContent className="py-12 text-center text-sm text-muted-foreground">No live employee streams right now.</CardContent></Card>
      )}
      {!loading && running.length > 0 && !focusedEmployee && (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-label="Running employee desktop streams">
          {running.map((employee) => (
            <Card key={employee.id} className="overflow-hidden">
              <CardHeader className="flex-row items-start justify-between space-y-0 pb-3">
                <div className="min-w-0">
                  <CardTitle className="truncate text-base">{employee.name}</CardTitle>
                  <CardDescription>{employee.role}</CardDescription>
                </div>
              </CardHeader>
              <CardContent className="pt-0">
                {employee.viewing_url ? (
                  <div className="relative aspect-video overflow-hidden rounded-md border bg-black">
                    <iframe
                      src={employee.viewing_url}
                      title={`${employee.name} employee desktop`}
                      inert
                      tabIndex={-1}
                      allow="fullscreen; autoplay 'none'; microphone 'none'; camera 'none'"
                      referrerPolicy="no-referrer"
                      className="pointer-events-none size-full border-0"
                    />
                    <button
                      type="button"
                      aria-label={`Focus ${employee.name} stream`}
                      className="absolute inset-0 cursor-zoom-in bg-transparent focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary"
                      onClick={() => { setFullscreenError(null); setFocusedId(employee.id) }}
                    />
                  </div>
                ) : (
                  <div className="grid aspect-video place-items-center rounded-md border bg-muted/40 px-4 text-center text-sm text-muted-foreground">
                    The running session has no viewer URL available.
                  </div>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {focusedEmployee?.viewing_url && (
        <div className="fixed inset-0 z-50 bg-background p-3 sm:p-5" role="dialog" aria-modal="true" aria-label={`${focusedEmployee.name} desktop stream`} onKeyDown={(event) => { if (event.key === "Escape") setFocusedId(null) }}>
          <div ref={focusedViewer} className="flex h-full flex-col gap-3">
            <div className="flex items-center justify-between gap-4">
              <div className="min-w-0">
                <h2 className="truncate text-lg font-semibold">{focusedEmployee.name}</h2>
                <p className="text-sm text-muted-foreground">{focusedEmployee.role}</p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Button size="sm" variant="outline" onClick={() => void enterFullscreen()}><Maximize2 className="size-4" /> Full screen</Button>
                <Button size="sm" variant="outline" onClick={() => setFocusedId(null)}>Back to grid</Button>
              </div>
            </div>
            {fullscreenError && <p className="text-sm text-destructive">{fullscreenError}</p>}
            <iframe
              src={focusedEmployee.viewing_url}
              title={`${focusedEmployee.name} employee desktop focused stream`}
              inert
              tabIndex={-1}
              allow="fullscreen; autoplay 'none'; microphone 'none'; camera 'none'"
              referrerPolicy="no-referrer"
              className="pointer-events-none min-h-0 flex-1 rounded-md border bg-black"
            />
          </div>
        </div>
      )}
    </section>
  )
}

const activityFilters = [["all", "All activity"], ["email", "Emails"], ["message", "Messages"], ["chatter", "Chatter"], ["action", "Company actions"]] as const

function ActivityPage() {
  const refreshKey = useRefreshKey()
  const [company, setCompany] = useState<Company | null>(null)
  const [activity, setActivity] = useState<ActivityEntry[]>([])
  const [metrics, setMetrics] = useState<ActivityMetrics | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [resending, setResending] = useState<string | null>(null)
  const [resendStatus, setResendStatus] = useState<{ id: string; message: string } | null>(null)
  const resendRequests = useRef(new Map<string, string>())
  const [activityWarning, setActivityWarning] = useState<string | null>(null)
  const [manualRefresh, setManualRefresh] = useState(0)
  const [refreshing, setRefreshing] = useState(false)
  const [activityParams, setActivityParams] = useSearchParams()
  const requestedFilter = activityParams.get("activity")
  const filter = requestedFilter && activityFilters.some(([value]) => value === requestedFilter) ? requestedFilter : "all"
  const companyRequest = useRef<Promise<Awaited<ReturnType<typeof getCompany>> | undefined> | null>(null)
  const activityRequest = useRef<Promise<Awaited<ReturnType<typeof getActivity>> | undefined> | null>(null)


  useEffect(() => {
    let active = true
    const pendingCompany = companyRequest.current?.then(() => active ? getCompany() : undefined, () => active ? getCompany() : undefined) ?? getCompany()
    const pendingActivity = activityRequest.current?.then(() => active ? getActivity() : undefined, () => active ? getActivity() : undefined) ?? getActivity()
    companyRequest.current = pendingCompany
    activityRequest.current = pendingActivity
    pendingCompany.then((result) => {
      if (!active || !result) return
      setCompany(result)
      setError(null)
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "Company status could not be loaded.")
    }).finally(() => {
      if (companyRequest.current === pendingCompany) companyRequest.current = null
    })
    pendingActivity.then((result) => {
      if (!active || !result) return
      setActivity(result.activity)
      setMetrics(result.metrics)
      setActivityWarning(result.error ?? null)
    }).catch((reason: unknown) => {
      if (active) setActivityWarning(reason instanceof Error ? reason.message : "Company activity could not be loaded.")
    }).finally(() => {
      if (activityRequest.current === pendingActivity) activityRequest.current = null
      if (active) {
        setLoading(false)
        setRefreshing(false)
      }
    })
    return () => { active = false }
  }, [refreshKey, manualRefresh])

  const refreshCompanyActivity = () => {
    setRefreshing(true)
    setManualRefresh((current) => current + 1)
  }

  const resend = async (entry: ActivityEntry) => {
    if (resending) return
    const requestId = resendRequests.current.get(entry.id) ?? crypto.randomUUID()
    resendRequests.current.set(entry.id, requestId)
    setResending(entry.id)
    setResendStatus(null)
    try {
      await resendActivityEmail(entry.id, requestId)
      resendRequests.current.delete(entry.id)
      setResendStatus({ id: entry.id, message: "Resent to Viv." })
      refreshCompanyActivity()
    } catch {
      setResendStatus({ id: entry.id, message: "Resend could not be confirmed." })
    } finally {
      setResending(null)
    }
  }

  const matchesFilter = (entry: ActivityEntry, value: string) =>
    value === "all" || entry.type === value || (value === "message" && entry.type === "chat")
  const activityDate = (entry: ActivityEntry) => {
    const timestamp = Date.parse(entry.timestamp ?? "")
    return Number.isFinite(timestamp) ? timestamp : Number.NEGATIVE_INFINITY
  }
  const filtered = activity.filter((entry) => matchesFilter(entry, filter))
    .sort((first, second) => activityDate(second) - activityDate(first))
  const backgroundErrors = company?.background?.errors ?? {}
  const globalBackgroundErrors = ([
    ["company_background", "Background worker"],
    ["company_feedback_reconcile", "Company feedback reconciliation"],
    ["native_finance_reconcile", "Finance synchronization"],
  ] as const).flatMap(([code, label]) => backgroundErrors[code]
    ? [{ code, label, detail: backgroundErrors[code].code }]
    : [])

  return (
    <section className="space-y-7">
      <div className="flex flex-col gap-2 border-b pb-6 sm:flex-row sm:items-end sm:justify-between">
        <div className="space-y-1.5">
          <h1 className="text-2xl font-semibold tracking-tight">Company activity</h1>
          <p className="text-sm text-muted-foreground">See what your company is sending, receiving and doing.</p>
        </div>
        <Button variant="outline" render={<Link to="/experiments" />}>Browse experiments <ArrowRight /></Button>
      </div>

      {globalBackgroundErrors.length > 0 && <Alert variant="destructive">
        <CircleAlert />
        <AlertTitle>Company background synchronization needs attention</AlertTitle>
        <AlertDescription>
          <ul className="list-disc space-y-1 pl-5">
            {globalBackgroundErrors.map(({ code, label, detail }) => <li key={code}>{label} ({detail})</li>)}
          </ul>
        </AlertDescription>
      </Alert>}
      {company?.error && <ErrorNotice message={company.error} title="Company needs attention" />}
      {error && <ErrorNotice message={error} title="Company status could not be loaded" />}



      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4" aria-label="Activity metrics">
        {[
          ["Emails sent", metrics?.emails_sent],
          ["Emails received", metrics?.emails_received],
          ["Chat messages", metrics?.messages],
          ["Open experiments", metrics?.open_experiments],
        ].map(([label, value]) => (
          <Card key={String(label)}>
            <CardHeader className="pb-1"><CardDescription>{label}</CardDescription></CardHeader>
            <CardContent><p className="text-3xl font-semibold tabular-nums">{typeof value === "number" ? value.toLocaleString() : "—"}</p><p className="mt-1 text-xs text-muted-foreground">Tracked across all experiments</p></CardContent>
          </Card>
        ))}
      </div>

      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="space-y-1"><h2 className="text-lg font-semibold">Live activity</h2></div>
          <Button variant="outline" size="sm" disabled={refreshing} aria-label="Refresh company activity" onClick={refreshCompanyActivity}>
            <RefreshCw className={refreshing ? "animate-spin" : ""} /> Refresh activity
          </Button>
        </div>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Filter activity">
          {activityFilters.map(([value, label]) => (
            <Button key={value} type="button" size="sm" variant={filter === value ? "default" : "outline"}
              aria-pressed={filter === value} onClick={() => setActivityParams((current) => {
                const next = new URLSearchParams(current)
                if (value === "all") next.delete("activity")
                else next.set("activity", value)
                return next
              }, { preventScrollReset: true })}>{label} ({activity.filter((entry) => matchesFilter(entry, value)).length})</Button>
          ))}
        </div>
      </div>

      {activityWarning && <ErrorNotice message={activityWarning} title="Some company activity could not be loaded" />}
      {loading ? <div className="space-y-3"><Skeleton className="h-24 w-full" /><Skeleton className="h-24 w-full" /></div> : filtered.length ? (
        <div key={filter} className="space-y-3">
          {filtered.map((entry) => {
            const isEmail = entry.type === "email"
            const isCommunication = isEmail || entry.type === "message" || entry.type === "chat" || entry.type === "chatter"
            const title = entry.title.trim()
            const content = entry.content?.trim()
            const summary = entry.summary?.trim()
            const showContent = Boolean(content && (isEmail || content !== title))
            const showSummary = Boolean(summary && summary !== title && !(showContent && content?.startsWith(summary)))
            const Icon = isEmail ? Mail : entry.type === "message" || entry.type === "chat" ? MessageCircle : Building2
            return <article key={JSON.stringify([entry.type, entry.id, entry.direction])} id={`activity-${entry.id}`} className="scroll-mt-6 rounded-xl border bg-card p-4 sm:p-5">
              <div className="flex items-start gap-3">
                <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-secondary text-secondary-foreground"><Icon className="size-4" /></span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                    <h3 className="font-medium">{entry.title}</h3>
                    <time className="text-xs text-muted-foreground">{entry.timestamp ? formatDate(entry.timestamp) : ""}</time>
                  </div>
                  {isEmail ? <div className="mt-3 space-y-2 text-sm">
                    <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 break-words">
                      <span className="font-medium">From</span><span>{entry.actor_name || entry.actor || "Unknown sender"}</span>
                      <span className="font-medium">To</span><span>{entry.recipient_name || entry.recipient || "Unknown recipient"}</span>
                    </div>
                    <div className="whitespace-pre-wrap break-words rounded-lg bg-muted/60 p-3 leading-6">
                      {content || "Message body was not captured."}
                    </div>
                    {entry.delivery_status && <p className="text-muted-foreground">{entry.delivery_status}</p>}
                    {Boolean(entry.attachments?.length) && <p>Attachments: {entry.attachments?.join(", ")}</p>}
                    {entry.native_document && <ActivityDocumentLink document={entry.native_document} />}
                  </div> : isCommunication ? <div className="mt-3 overflow-x-auto rounded-lg border bg-muted/60">
                    <table className="w-full text-left text-sm">
                      <thead className="border-b">
                        <tr>
                          <th scope="col" className="p-3 font-medium">Direction</th>
                          <th scope="col" className="p-3 font-medium">From / To</th>
                          <th scope="col" className="p-3 font-medium">Message</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr className="align-top">
                          <td className="p-3 font-medium">{entry.direction === "received" ? "Received" : entry.direction === "sent" ? "Sent" : entry.delivery_status || "Recorded"}
                            {entry.direction && entry.delivery_status && <p className="mt-1 text-xs font-normal text-muted-foreground">{entry.delivery_status}</p>}
                          </td>
                          <td className="break-words p-3">
                            <p><span className="font-medium">From</span> {entry.actor_name || entry.actor || "Unknown sender"}</p>
                            {entry.recipient && <p className="mt-1"><span className="font-medium">To</span> {entry.recipient_name || entry.recipient}</p>}
                          </td>
                          <td className="w-full min-w-48 break-words p-3">
                            <p className="whitespace-pre-wrap leading-6">{content ? formatActivityText(content) : "Message body was not captured."}</p>
                            {Boolean(entry.attachments?.length) && <p className="mt-2">Attachments: {entry.attachments?.join(", ")}</p>}
                            {entry.native_document && <ActivityDocumentLink document={entry.native_document} />}
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div> : <>
                    {entry.actor && <p className="mt-1 text-xs text-muted-foreground">{entry.actor}</p>}
                    {showSummary && summary && <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-foreground/85">{formatActivityText(summary)}</p>}
                    {showContent && content && <p className="mt-3 whitespace-pre-wrap break-words rounded-lg bg-muted/60 p-3 text-sm leading-6">{formatActivityText(content)}</p>}
                    {entry.native_document && <ActivityDocumentLink document={entry.native_document} />}
                  </>}
                  {entry.can_resend && <div className="mt-3 flex items-center gap-3">
                    <Button size="sm" variant="outline" disabled={resending !== null} onClick={() => void resend(entry)}>
                      {resending === entry.id ? "Resending…" : "Resend to Viv"}
                    </Button>
                    <span role="status" className="text-sm text-muted-foreground">
                      {resendStatus?.id === entry.id && resendStatus.message}
                    </span>
                  </div>}
                  {entry.coffee_update && <CoffeeUpdateDetails update={entry.coffee_update} />}
                  {entry.run_id && <Link className="mt-3 inline-flex items-center gap-1 text-sm text-primary hover:underline" to={`/runs/${encodeURIComponent(entry.run_id)}`}>View experiment <ArrowRight className="size-3.5" /></Link>}
                </div>
              </div>
            </article>
          })}
        </div>
      ) : (
        <div className="rounded-xl border border-dashed px-6 py-14 text-center">
          <MessageCircle className="mx-auto size-8 text-muted-foreground" />
          <p className="mt-3 font-medium">{activity.length ? "No activity matches this filter" : "Nothing has happened yet"}</p>
          <p className="mt-1 text-sm text-muted-foreground">Run an experiment to introduce a problem into the company.</p>
          <Button className="mt-5" render={<Link to="/experiments" />}>Browse experiments <ArrowRight /></Button>
        </div>
      )}
    </section>
  )
}

function ActivityDocumentLink({ document }: { document: NonNullable<ActivityEntry["native_document"]> }) {
  const [url, setUrl] = useState<string | null>(null)
  const [unavailable, setUnavailable] = useState(false)
  const label = `${document.doctype} ${document.name}`
  return <details className="mt-2 text-sm" onToggle={(event) => {
    if (!event.currentTarget.open || url) return
    void getCompanyTools().then((tools) => {
      const base = tools.find((tool) => tool.id === "erpnext")?.url
      if (!base) { setUnavailable(true); return }
      const slug = document.doctype.toLowerCase().replaceAll(" ", "-")
      setUrl(new URL(`/app/${encodeURIComponent(slug)}/${encodeURIComponent(document.name)}`, base).href)
    }).catch(() => setUnavailable(true))
  }}>
    <summary className="cursor-pointer">{label}</summary>
    {url ? <a href={url} target="_blank" rel="noreferrer" className="underline">Open in ERPNext</a>
      : <p>{unavailable ? "ERPNext link unavailable" : "Loading ERPNext link…"}</p>}
  </details>
}

function CoffeeUpdateDetails({ update }: { update: NonNullable<ActivityEntry["coffee_update"]> }) {
  const [erpUrl, setErpUrl] = useState<string | null>(null)
  const hasRecords = update.messages.some((message) => message.doctype && message.name)
    || update.records?.some((record) => record.doctype && record.name)
  const recordLink = (doctype: string, name: string) => {
    const label = `${doctype} ${name}`
    if (!erpUrl) return label
    const slug = doctype.toLowerCase().replaceAll(" ", "-")
    return <a className="underline" href={new URL(`/app/${encodeURIComponent(slug)}/${encodeURIComponent(name)}`, erpUrl).href} target="_blank" rel="noreferrer">{label}</a>
  }
  return <details className="mt-3 text-sm" onToggle={(event) => {
    if (event.currentTarget.open && hasRecords && !erpUrl) {
      void getCompanyTools().then((tools) => setErpUrl(tools.find((tool) => tool.id === "erpnext")?.url ?? null))
        .catch((error) => console.error("Could not load ERPNext link.", error))
    }
  }}>
    <summary className="cursor-pointer font-medium underline">View update details</summary>
    <div className="mt-3 space-y-3">
      <p>Simulation advanced through {formatDate(update.cursor)}.</p>
      {update.messages.length === 0 && <p>No incoming marketplace messages recorded.</p>}
      {update.messages.map((message) => <div key={message.event_id} className="border-t pt-3">
        <p className="font-medium">{message.title}</p>
        <p className="text-muted-foreground">{message.sender} · {formatDate(message.timestamp)}</p>
        <p className="my-2 whitespace-pre-wrap break-words">{message.body}</p>
        <p>{message.delivery_status === "delivered" ? "Email delivery confirmed" : message.delivery_status === "not_queued" ? "Email not queued" : "Email delivery awaiting confirmation"}{message.recipient && ` · ${message.recipient}`}</p>
        {message.message_id && <p className="break-all text-xs text-muted-foreground">{message.message_id}</p>}
        {message.doctype && message.name && <p>{recordLink(message.doctype, message.name)}</p>}
        <a className="underline" href={`#activity-${message.event_id}`}>View source event</a>
        {message.replies.map((reply, index) => <div key={index} className="mt-2 border-l pl-3">
          <p className="font-medium">Marketplace reply · {reply.status}</p>
          <p>{reply.title}</p>
          <p className="whitespace-pre-wrap break-words">{reply.body}</p>
        </div>)}
      </div>)}
      {update.records === null ? <p>ERP action links were not recorded for this historical update.</p>
        : update.records.length === 0 ? <p>No ERP actions recorded.</p>
          : <ul className="list-disc pl-5">{update.records.map((record) => <li key={record.source_id}>
            {record.doctype && record.name ? recordLink(record.doctype, record.name) : "ERP action pending reconciliation"}
          </li>)}</ul>}
    </div>
  </details>
}

function CodexConnectionCard() {
  const [auth, setAuth] = useState<CodexAuthStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [connecting, setConnecting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    getCodexAuthStatus().then((status) => {
      if (active) {
        setAuth(status)
        setError(null)
      }
    }).catch((reason) => {
      if (active) setError(reason instanceof Error ? reason.message : "Codex connection status could not be loaded.")
    }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    const loginId = auth?.login_id
    if (!loginId || !["starting", "pending"].includes(auth?.state ?? "")) return
    let active = true
    let pending = false
    const refresh = async () => {
      if (pending) return
      pending = true
      try {
        const status = await getCodexDeviceAuthorization(loginId)
        if (active) {
          setAuth(status)
          setError(null)
        }
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : "Codex sign-in status could not be retrieved.")
      } finally {
        pending = false
      }
    }
    const interval = window.setInterval(() => void refresh(), 3000)
    return () => { active = false; window.clearInterval(interval) }
  }, [auth?.login_id, auth?.state])

  const connect = async () => {
    setConnecting(true)
    setError(null)
    try {
      const status = await startCodexDeviceAuthorization()
      setAuth(status)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Codex sign-in could not be started.")
    } finally {
      setConnecting(false)
    }
  }

  const pending = auth?.state === "starting" || auth?.state === "pending"
  const connected = auth?.authenticated === true && !pending

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="font-medium">Codex</p>
          <p className="text-sm text-muted-foreground">
            {connected ? "Connected" : pending ? "Complete sign-in on the linked page below." : "Use the company’s Codex account for experiments."}
          </p>
        </div>
        {connected ? <div className="flex shrink-0 items-center gap-3">
          <Badge variant="secondary">Connected</Badge>
          <Button size="sm" variant="outline" onClick={() => void connect()} disabled={connecting}>
            {connecting ? <LoaderCircle className="animate-spin" /> : null}
            Sign in with another account
          </Button>
        </div> : (
          <Button onClick={() => void connect()} disabled={loading || connecting || pending}>
            {connecting || pending ? <LoaderCircle className="animate-spin" /> : null}
            {pending ? "Waiting for approval" : connecting ? "Starting sign-in" : "Connect Codex"}
          </Button>
        )}
      </CardContent>
      {(error || auth?.error) && <CardContent className="border-t pt-3">
        {(error || auth?.error) && <ErrorNotice message={error ?? `Codex sign-in failed (${auth?.error}).`} title="Codex connection needs attention" />}
      </CardContent>}
      {pending && auth?.user_code && auth.verification_url && <CardContent className="border-t pt-3">
        <div className="rounded-lg border bg-muted/40 p-4">
          <p className="text-sm">Open the sign-in page and enter this one-time code:</p>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <code className="rounded bg-background px-3 py-2 text-lg font-semibold tracking-wider">{auth.user_code}</code>
            <a className="inline-flex items-center gap-1 text-sm font-medium text-primary hover:underline" href={auth.verification_url} target="_blank" rel="noreferrer">
              Open sign-in page <ExternalLink className="size-3.5" />
            </a>
          </div>
        </div>
      </CardContent>}
    </Card>
  )
}

function SavedNativeEvaluations() {
  const refreshKey = useRefreshKey()
  const [nativeEvaluations, setNativeEvaluations] = useState<Record<string, CompanyNativeEvaluationProvider> | null>(null)
  const [nativeEvaluationError, setNativeEvaluationError] = useState<string | null>(null)
  const [evaluatingProvider, setEvaluatingProvider] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    getCompanyEvaluations().then((evaluations) => {
      if (active) {
        setNativeEvaluations(evaluations)
        setNativeEvaluationError(null)
      }
    }).catch((reason) => {
      if (active) setNativeEvaluationError(reason instanceof Error ? reason.message : "Saved native evaluations could not be loaded.")
    })
    return () => { active = false }
  }, [refreshKey])

  const saveNativeEvaluation = async (providerId: CompanyNativeEvaluation["provider_id"]) => {
    const keyName = `experiments.company-evaluation.${providerId}`
    let key: string | null = null
    try {
      key = window.sessionStorage.getItem(keyName)
      if (!key) {
        key = crypto.randomUUID()
        window.sessionStorage.setItem(keyName, key)
      }
    } catch {
      setNativeEvaluationError("This browser could not preserve a retry key. Enable session storage before saving an evaluation.")
      return
    }
    setEvaluatingProvider(providerId)
    try {
      await startCompanyEvaluation(providerId, key)
      window.sessionStorage.removeItem(keyName)
      setNativeEvaluations(await getCompanyEvaluations())
      setNativeEvaluationError(null)
    } catch (reason) {
      setNativeEvaluationError(reason instanceof Error ? reason.message : "The evaluation result could not be confirmed. Retry to reconcile it.")
    } finally {
      setEvaluatingProvider(null)
    }
  }

  return (
      <Card>
        <CardHeader>
          <CardTitle>Saved native evaluations</CardTitle>
          <CardDescription>Company-wide snapshots of the background benchmark worlds. These results are not attributed to an experiment.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          {nativeEvaluationError && <ErrorNotice message={nativeEvaluationError} title="Evaluation history needs attention" />}
          <div className="grid gap-4 lg:grid-cols-2">
            {([["coffeebench", "CoffeeBench"], ["ecommercebench", "E-CommerceBench"]] as const).map(([providerId, label]) => {
              const provider = nativeEvaluations?.[providerId]
              return <section key={providerId} className="rounded-lg border p-4" aria-label={`${label} saved evaluations`}>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div><h3 className="font-medium">{label}</h3><p className="mt-1 text-xs text-muted-foreground">Company-wide · native scale · no combined score</p></div>
                  <Button size="sm" variant="outline" disabled={evaluatingProvider !== null} onClick={() => void saveNativeEvaluation(providerId)}>
                    {evaluatingProvider === providerId ? <LoaderCircle className="animate-spin" /> : null}
                    Save snapshot
                  </Button>
                </div>
                {provider?.error && <p className="mt-3 text-sm text-destructive">History unavailable ({provider.error.code}).</p>}
                {!provider?.error && (provider?.evaluations.length ?? 0) === 0 && <p className="mt-3 text-sm text-muted-foreground">No saved evaluation yet.</p>}
                {!provider?.error && provider && provider.evaluations.length > 0 && <div className="mt-4 space-y-3">
                  {provider.latest && <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Latest saved</p>}
                  {provider.evaluations.map((evaluation) => <CompanyEvaluationEntry key={evaluation.evaluation_id} evaluation={evaluation} latest={evaluation.evaluation_id === provider.latest?.evaluation_id} />)}
                </div>}
              </section>
            })}
          </div>
        </CardContent>
      </Card>
  )
}

function HelpPage() {
  return (
    <section className="space-y-5">
      <h1 className="text-2xl font-semibold tracking-tight">Help</h1>
      <nav className="flex flex-wrap gap-2" aria-label="Help pages">
        <Button variant="outline" render={<Link to="/help/company-environment" />}>Company environment</Button>
        <Button variant="outline" render={<Link to="/help/context" />}>Codex connection</Button>
        <Button variant="outline" render={<Link to="/help/native-evaluations" />}>Native evaluations</Button>
        <Button variant="outline" render={<Link to="/help/company-tools" />}>Company tools</Button>
      </nav>
    </section>
  )
}

function HelpSection({ title, children }: { title: string, children: React.ReactNode }) {
  return (
    <section className="space-y-5">
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      {children}
    </section>
  )
}

function CompanyToolsPage() {
  const [companyTools, setCompanyTools] = useState<CompanyToolLink[]>([])
  const [companyToolsError, setCompanyToolsError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    getCompanyTools().then((tools) => {
      if (active) setCompanyTools(tools)
    }).catch((reason: unknown) => {
      if (active) setCompanyToolsError(reason instanceof Error && reason.message.includes("company_tools_coordinator_unavailable")
        ? "The coordinator could not provide the selected company applications. Restore its connection and reload to retrieve Company tools."
        : "The deployment’s Company tools URL mapping is missing or invalid. An operator needs to correct it.")
    })
    return () => { active = false }
  }, [])

  return (
    <HelpSection title="Company tools">
      <Card>
        <CardContent className="flex flex-wrap gap-2">
          {companyToolsError ? <ErrorNotice message={companyToolsError} title="Company tools unavailable" /> : companyTools.map((tool) => (
            <Button key={tool.id} variant="outline" render={<a href={tool.url} target="_blank" rel="noopener noreferrer" />}>
              {tool.name}<ExternalLink className="size-4" />
            </Button>
          ))}
        </CardContent>
      </Card>
    </HelpSection>
  )
}

const coffeePaymentWindows = [
  ["friday", "Friday payments", "The customer processes payments only on Fridays."],
  ["morning", "Payments before 10 a.m.", "The customer processes payments before 10 a.m. If Friday payments are also enabled, the Friday batch schedule takes precedence."],
  ["financing", "Customer financing requests", "The customer asks for financing on a coffee order. Finance hands off the request so a Lending application can be submitted."],
  ["batch_cutoff", "Weekly batch cutoff", "The customer pays in a Friday batch starting at 10 a.m. Invoices received at or after the cutoff wait until the following Friday."],
  ["financial_distress", "Financial distress", "The customer asks for longer terms and delays payment by 7 to 28 days after the due date as earlier invoices accumulate."],
  ["higher_credit_limit", "Requests a higher credit limit", "The customer asks for a higher credit limit when a new order exceeds their existing limit."],
  ["partial_payment", "Partial payment", "The customer pays half the outstanding invoice and disputes the rest until a linked credit note is submitted."],
  ["wrong_recipient", "Wrong billing recipient", "The first invoice goes to internal Finance. The customer waits for it to be resent to Accounts Payable."],
  ["receiving_mismatch", "Receiving mismatch", "The customer receives fewer units than ordered and disputes the missing amount until a linked credit note is submitted."],
  ["missing_po", "Missing purchase order reference", "The customer holds payment until they receive an invoice with the requested purchase order reference."],
  ["reminder", "Payment after a reminder", "The customer waits for a payment reminder delivered as a reply on the invoice email thread."],
] as const

const otherCoffeeBehaviours = [
  ["Prompt payment", "Native behaviour"],
  ["Temporary cash shortage", "Native behaviour"],
  ["Requests longer credit terms", "Native behaviour"],
  ["Bankruptcy", "Native behaviour"],
] as const

const ecommerceBehaviours = [
  ["cancellation_after_picking", "Cancellation after picking"],
  ["stockout", "Partial shipment or stockout"],
  ["parcel_loss", "Lost or damaged parcel"],
  ["partial_return", "Partial return"],
  ["refund_after_payout", "Refund after payout"],
  ["payout_mismatch", "Marketplace payout mismatch"],
] as const

function BenchmarkSettingsPage({ ecommerce = false }: { ecommerce?: boolean }) {
  const title = ecommerce ? "E-CommerceBench behaviours" : "CoffeeBench behaviours"
  const behaviours = ecommerce ? ecommerceBehaviours : coffeePaymentWindows
  const load = ecommerce ? getEcommerceBehaviours : getCoffeePaymentWindows
  const save = ecommerce ? setEcommerceBehaviour : setCoffeePaymentWindow
  const [settings, setSettings] = useState<CoffeePaymentWindows | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let active = true
    load().then(
      (result) => { if (active) setSettings(result) },
      () => { if (active) setError("Behaviour settings could not be loaded.") },
    )
    return () => { active = false }
  }, [load])

  async function update(behaviour: string, enabled: boolean) {
    setSaving(true)
    setError(null)
    try {
      setSettings(await save(behaviour, enabled))
    } catch {
      setError("The change could not be confirmed. Reload to check the saved settings.")
    } finally {
      setSaving(false)
    }
  }

  return <section className="space-y-5">
    <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
    <Card>
      <CardHeader>
        <CardTitle>Behaviour switches</CardTitle>
        {!ecommerce && <CardDescription>Payment windows use simulated UTC.</CardDescription>}
      </CardHeader>
      <CardContent className="space-y-4 overflow-x-auto">
        {error && <ErrorNotice message={error} title={title} />}
        {!settings && !error && <p role="status" className="text-sm text-muted-foreground">Loading behaviours…</p>}
        {settings && <ul className="divide-y">
          {behaviours.map(([behaviour, title, description]) => <li key={behaviour} className="flex items-center justify-between gap-4 p-3">
            <div>
              <div>{title}</div>
              {description && <p id={`behaviour-${behaviour}`} className="text-sm text-muted-foreground">{description}</p>}
            </div>
            <Switch.Root aria-label={title} aria-describedby={description ? `behaviour-${behaviour}` : undefined} checked={settings.includes(behaviour)}
              disabled={saving} onCheckedChange={(checked) => void update(behaviour, checked)}
              className="relative inline-flex h-6 w-11 shrink-0 rounded-full bg-input data-[checked]:bg-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50">
              <Switch.Thumb className="block size-5 translate-x-0.5 rounded-full bg-background transition-transform data-[checked]:translate-x-5" />
            </Switch.Root>
          </li>)}
        </ul>}
      </CardContent>
    </Card>
    {!ecommerce && <Card>
      <CardHeader><CardTitle>Other scenarios</CardTitle></CardHeader>
      <CardContent>
        <ul className="divide-y">
          {otherCoffeeBehaviours.map(([title, status]) => <li key={title} className="flex flex-wrap items-center justify-between gap-2 p-3">
            <span>{title}</span>
            <Badge variant="secondary">{status}</Badge>
          </li>)}
        </ul>
      </CardContent>
    </Card>}
  </section>
}

function BackgroundSimulations() {
  const refreshKey = useRefreshKey()
  const [background, setBackground] = useState<BenchmarkStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [backgroundControlBusy, setBackgroundControlBusy] = useState<Record<string, boolean>>({})
  const [backgroundControlErrors, setBackgroundControlErrors] = useState<Record<string, string>>({})

  useEffect(() => {
    let active = true
    getBenchmarkStatus().then((value) => {
      if (active) { setBackground(value); setError(null) }
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "Company status could not be loaded.")
    })
    return () => { active = false }
  }, [refreshKey])

  const setBackgroundProvider = async (providerId: "coffeebench" | "ecommercebench", enabled: boolean) => {
    setBackgroundControlBusy((current) => ({ ...current, [providerId]: true }))
    setBackgroundControlErrors((current) => ({ ...current, [providerId]: "" }))
    try {
      const confirmed = await setBackgroundProviderEnabled(providerId, enabled)
      setBackground((current) => {
        if (!current) return current
        const errors = { ...current.errors }
        if (confirmed.status !== "error") delete errors[providerId]
        return {
          ...current, errors,
          providers: { ...current.providers, [providerId]: { ...current.providers[providerId], ...confirmed } },
        }
      })
    } catch (reason) {
      setBackgroundControlErrors((current) => ({ ...current, [providerId]: reason instanceof Error ? reason.message : `${providerId} control could not be confirmed. Refresh to reconcile the requested state.` }))
    } finally {
      setBackgroundControlBusy((current) => ({ ...current, [providerId]: false }))
    }
  }

  return <div className="space-y-4">
    {error && <ErrorNotice message={error} title="Benchmark status" />}
      <Card>
        <CardHeader>
          <CardTitle>Benchmark controls</CardTitle>
        </CardHeader>
        <CardContent className="divide-y">
          {([['coffeebench', 'CoffeeBench'], ['ecommercebench', 'E-CommerceBench']] as const).map(([providerId, label]) => {
            const provider = background?.providers[providerId]
            const enabled = provider?.enabled
            const issue = background?.errors[providerId] ?? background?.errors.configuration
            return <div key={providerId} className="py-3 first:pt-0 last:pb-0">
              <div className="flex items-center justify-between gap-4">
                <div className="flex items-center gap-3">
                  <span id={`${providerId}-label`} className="font-medium">{label}</span>
                  <span className="text-sm text-muted-foreground">{enabled === undefined ? (error || issue ? null : "Loading…") : enabled ? "On" : "Off"}</span>
                </div>
                {enabled !== undefined && <Switch.Root
                  aria-labelledby={`${providerId}-label`}
                  checked={enabled === true}
                  disabled={backgroundControlBusy[providerId] || provider?.status === "stopping" || (enabled === false && provider?.in_flight === true)}
                  onCheckedChange={(checked) => void setBackgroundProvider(providerId, checked)}
                  className="relative inline-flex h-6 w-11 shrink-0 items-center rounded-full bg-input transition-colors data-[checked]:bg-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Switch.Thumb className="block size-5 translate-x-0.5 rounded-full bg-background shadow-sm transition-transform data-[checked]:translate-x-5" />
                </Switch.Root>}
              </div>
              {providerId === "coffeebench" && <Link to="/experiments/coffeebench/settings" className="mt-2 inline-block text-sm underline">Behaviour settings</Link>}
              {provider?.status === "starting" && <p className="mt-2 text-sm text-muted-foreground">Starting…</p>}
              {(provider?.status === "stopping" || (enabled === false && provider?.in_flight)) && <p className="mt-2 text-sm text-muted-foreground">Finishing current work…</p>}
              {issue && <p className="mt-2 text-sm text-destructive">Needs attention ({issue.code}).</p>}
              {!issue && provider?.status === "error" && <p className="mt-2 text-sm text-destructive">Needs attention.</p>}
              {backgroundControlErrors[providerId] && <p className="mt-2 text-sm text-destructive">{backgroundControlErrors[providerId]}</p>}
            </div>
          })}
        </CardContent>
      </Card>
  </div>
}

function LibraryPage() {
  const refreshKey = useRefreshKey()
  const [importRevision, setImportRevision] = useState(0)
  const [experiments, setExperiments] = useState<Experiment[]>([])
  const [catalogWarning, setCatalogWarning] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  const search = searchParams.get("q") ?? ""
  const selectedTypes = useMemo(() => searchParams.getAll("type"), [searchParams])
  const selectedBenchmarks = useMemo(() => searchParams.getAll("benchmark"), [searchParams])
  const benchmarks = [...new Set(experiments.flatMap((experiment) => experiment.benchmarks ?? ["The Agent Company"]))].sort()
  const types = [...new Set(experiments.map((experiment) => experiment.role))].sort()

  function updateFilters(search: string, selected: string[], selectedBenches = selectedBenchmarks) {
    const next = new URLSearchParams(searchParams)
    next.delete("q")
    next.delete("type")
    next.delete("benchmark")
    if (search) next.set("q", search)
    selected.forEach((type) => next.append("type", type))
    selectedBenches.forEach((benchmark) => next.append("benchmark", benchmark))
    setSearchParams(next, { replace: true })
  }

  useEffect(() => {
    let active = true
    setLoading(true)
    getCatalog()
      .then(({ experiments: items, error: importedError }) => {
        if (active) {
          setExperiments(items)
          setCatalogWarning(importedError ? `Imported experiments are unavailable (${importedError.code}). Built-in experiments remain available. Refresh to retry.` : null)
          setError(null)
        }
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "The experiment library could not be loaded.")
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => { active = false }
  }, [refreshKey, importRevision])

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase()
    return experiments.filter((experiment) =>
      (!selectedTypes.length || selectedTypes.includes(experiment.role)) &&
      (!selectedBenchmarks.length || (experiment.benchmarks ?? ["The Agent Company"]).some((benchmark) => selectedBenchmarks.includes(benchmark))) &&
      [experiment.title, experiment.summary, experiment.role, ...experiment.dependencies].join(" ").toLowerCase().includes(term),
    )
  }, [experiments, search, selectedTypes, selectedBenchmarks])

  return (
    <section className="space-y-7">
      <div className="flex flex-col gap-5 border-b pb-6 sm:flex-row sm:items-end sm:justify-between">
        <div className="space-y-1.5">
          <h1 className="text-2xl font-semibold tracking-tight">Experiment library</h1>
        </div>
        <div className="flex w-full items-center gap-2 sm:w-80">
          <div className="relative w-full">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
            <Input value={search} onChange={(event) => updateFilters(event.target.value, selectedTypes)} placeholder="Search tasks" className="h-10 pl-9" aria-label="Search experiments" />
          </div>
        </div>
      </div>
      <nav className="flex flex-wrap gap-2" aria-label="Simulation settings">
        <Button variant="outline" render={<Link to="/experiments/background" />}>Background simulations</Button>
        <Button variant="outline" render={<Link to="/experiments/coffeebench/settings" />}>CoffeeBench behaviours</Button>
      </nav>
      <CompanyPackImport onImported={() => setImportRevision((value) => value + 1)} />

      <fieldset className="space-y-3">
        <legend className="text-sm font-medium">Benchmark</legend>
        <div className="flex flex-wrap gap-2">
          {benchmarks.map((benchmark) => (
            <Button key={benchmark} variant={selectedBenchmarks.includes(benchmark) ? "default" : "outline"}
              aria-pressed={selectedBenchmarks.includes(benchmark)}
              onClick={() => updateFilters(search, selectedTypes, selectedBenchmarks.includes(benchmark) ? selectedBenchmarks.filter((item) => item !== benchmark) : [...selectedBenchmarks, benchmark])}>
              {benchmark}
            </Button>
          ))}
          {selectedBenchmarks.length > 0 && <Button variant="ghost" onClick={() => updateFilters(search, selectedTypes, [])}>Clear benchmarks</Button>}
        </div>
        <p className="text-xs text-muted-foreground">Select several benchmarks to show experiments using any of them.</p>
      </fieldset>

      <fieldset className="space-y-3">
        <legend className="text-sm font-medium">Business area</legend>
        <div className="flex flex-wrap gap-2">
          {types.map((type) => (
            <Button key={type} variant={selectedTypes.includes(type) ? "default" : "outline"}
              aria-pressed={selectedTypes.includes(type)}
              onClick={() => updateFilters(search, selectedTypes.includes(type) ? selectedTypes.filter((item) => item !== type) : [...selectedTypes, type])}>
              {type}
            </Button>
          ))}
          {selectedTypes.length > 0 && <Button variant="ghost" onClick={() => updateFilters(search, [])}>Clear types</Button>}
        </div>
        <p className="text-xs text-muted-foreground">Select one or more types. No selection shows all types.</p>
      </fieldset>

      {catalogWarning && <ErrorNotice message={catalogWarning} title="Imported library unavailable" />}
      {error && <ErrorNotice message={error} title="Library unavailable" />}
      {loading ? <LoadingCards /> : !error && filtered.length > 0 ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((experiment) => (
            <Link key={experiment.id} to={`/experiments/${encodeURIComponent(experiment.id)}`} className="group rounded-xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
              <Card className="h-full min-h-52 transition-colors group-hover:ring-primary/30 group-focus-visible:ring-primary">
                <CardHeader className="gap-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex flex-wrap gap-2">
                      <Badge variant="secondary" className="font-medium">{experiment.role}</Badge>
                    </div>
                    <ArrowUpRight className="size-4 text-muted-foreground transition-transform group-hover:-translate-y-0.5 group-hover:translate-x-0.5" aria-hidden="true" />
                  </div>
                  <CardTitle className="text-base leading-snug">{experiment.title}</CardTitle>
                  <CardDescription className="line-clamp-3 leading-relaxed">{experiment.summary}</CardDescription>
                </CardHeader>
                <CardFooter className="mt-auto justify-between text-xs text-muted-foreground">
                  <span>{experiment.dependencies.length} {experiment.dependencies.length === 1 ? "tool" : "tools"}</span>
                  <span className="inline-flex items-center gap-1 text-foreground">View task <ArrowRight className="size-3.5" /></span>
                </CardFooter>
              </Card>
            </Link>
          ))}
        </div>
      ) : !error ? (
        <div className="rounded-xl border border-dashed px-6 py-14 text-center">
          <p className="font-medium">{search || selectedTypes.length || selectedBenchmarks.length ? "No matching tasks" : "No tasks available"}</p>
          <p className="mt-1 text-sm text-muted-foreground">{search || selectedTypes.length || selectedBenchmarks.length ? "Try a different search or clear the type filters." : "Tasks will appear here when available."}</p>
        </div>
      ) : null}
      {!loading && !error && experiments.length > 0 && (
        <p className="text-xs text-muted-foreground">Showing {filtered.length} of {experiments.length} tasks</p>
      )}
    </section>
  )
}

function ExperimentPage() {
  const { experimentId = "" } = useParams()
  const navigate = useNavigate()
  const [experiment, setExperiment] = useState<Experiment | null>(null)
  const [session, setSession] = useState<Session | null>(null)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setLoading(true)
    Promise.all([getCatalog(), getSession()])
      .then(([{ experiments: items }, currentSession]) => {
        if (!active) return
        const selected = items.find((item) => item.id === experimentId) ?? null
        setExperiment(selected)
        setSession(currentSession)
        setError(null)
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : "The task could not be loaded.")
      })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [experimentId])

  const submit = async () => {
    if (!session) return
    setSubmitting(true)
    setError(null)
    try {
      const result = await startRun(experimentId, session)
      navigate(result.url)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The assignment could not be sent.")
      setSubmitting(false)
    }
  }

  if (loading) return <PageLoading />
  if (error && !experiment) return <ErrorNotice message={error} title="Task unavailable" />
  if (!experiment) return <NotFound />

  return (
    <section className="space-y-6">
      <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
        <Link to="/experiments" className="hover:text-foreground">Experiments</Link><span aria-hidden="true">/</span><span className="text-foreground">{experiment.title}</span>
      </div>
      <div className="space-y-4 border-b pb-6">
        <div className="flex flex-wrap items-center gap-2"><Badge variant="secondary">{experiment.role}</Badge><span className="text-xs text-muted-foreground">{experiment.dependencies.length} {experiment.dependencies.length === 1 ? "tool" : "tools"}</span></div>
        <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">{experiment.title}</h1>
        <p className="max-w-3xl text-base leading-relaxed text-muted-foreground">{experiment.summary}</p>
      </div>
      {error && <ErrorNotice message={error} title="Experiment could not be started" />}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <Card>
          <CardHeader className="border-b">
            <CardTitle>Task overview</CardTitle>
            <CardDescription>Business goal and expected outcome</CardDescription>
          </CardHeader>
          <CardContent className="py-5">
            <div className="whitespace-pre-wrap break-words text-sm leading-7 text-foreground/90">{experiment.display_description}</div>
          </CardContent>
        </Card>
        <aside className="space-y-4">
          <Card>
            <CardHeader>
            <CardTitle className="text-base">Run this task</CardTitle>
              <CardDescription>Give this assignment to your company and follow its messages and progress in Activity.</CardDescription>
            </CardHeader>
            {experiment.dependencies.length > 0 && (
              <CardContent className="space-y-2">
                <Separator />
                <p className="pt-2 text-xs font-medium text-muted-foreground">Available tools</p>
                <div className="flex flex-wrap gap-1.5">{experiment.dependencies.map((dependency) => <Badge key={dependency} variant="outline" className="font-normal">{dependency}</Badge>)}</div>
              </CardContent>
            )}
            <CardFooter>
              <Button className="w-full" size="lg" onClick={submit} disabled={submitting || !session}>
                {submitting ? <><LoaderCircle className="animate-spin" /> Sending</> : <>Run experiment <ArrowUpRight /></>}
              </Button>
            </CardFooter>
          </Card>
          <Button variant="outline" className="w-full" render={<Link to="/runs" />}>View runs <ArrowRight /></Button>
        </aside>
      </div>
    </section>
  )
}

function RunsPage() {
  const refreshKey = useRefreshKey()
  const [runs, setRuns] = useState<ExperimentRun[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const runsRequest = useRef<Promise<Awaited<ReturnType<typeof getRuns>> | undefined> | null>(null)

  useEffect(() => {
    let active = true
    const pending = runsRequest.current?.then(() => active ? getRuns() : undefined, () => active ? getRuns() : undefined) ?? getRuns()
    runsRequest.current = pending
    pending
      .then((items) => { if (active && items) { setRuns(items); setError(null) } })
      .catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : "Runs could not be loaded.") })
      .finally(() => {
        if (runsRequest.current === pending) runsRequest.current = null
        if (active) setLoading(false)
      })
    return () => { active = false }
  }, [refreshKey])

  return (
    <section className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4 border-b pb-6">
        <div className="space-y-1.5"><h1 className="text-2xl font-semibold tracking-tight">Runs</h1><p className="text-sm text-muted-foreground">Workplace assignments and company simulations, with their current status.</p></div>
        <Button variant="outline" render={<Link to="/experiments" />}>Browse experiments <ArrowRight /></Button>
      </div>
      {error && <ErrorNotice message={error} title="Run tracker unavailable" />}
      {loading ? <div className="space-y-3"><Skeleton className="h-10 w-full" /><Skeleton className="h-14 w-full" /><Skeleton className="h-14 w-full" /></div> : !error && runs.length === 0 ? (
        <div className="rounded-xl border border-dashed px-6 py-14 text-center">
          <Clock3 className="mx-auto size-8 text-muted-foreground" />
          <p className="mt-3 font-medium">No runs yet</p>
          <p className="mt-1 text-sm text-muted-foreground">Choose an experiment to give your company its first assignment.</p>
          <Button className="mt-5" render={<Link to="/experiments" />}>Browse experiments <ArrowRight /></Button>
        </div>
      ) : !error ? (
        <Card className="py-0">
          <Table>
            <TableHeader><TableRow className="hover:bg-transparent"><TableHead>Task</TableHead><TableHead>Started</TableHead><TableHead>Status</TableHead><TableHead className="w-12" /></TableRow></TableHeader>
            <TableBody>
              {runs.map((run) => (
                <TableRow key={run.id}>
                  <TableCell className="min-w-52 whitespace-normal">
                    <Link to={`/runs/${encodeURIComponent(run.id)}`} className="font-medium hover:underline">{run.title}</Link>
                  </TableCell>
                  <TableCell className="whitespace-nowrap text-sm text-muted-foreground">{formatDate(run.created_at)}</TableCell>
                  <TableCell><StatusBadge status={run.status} /></TableCell>
                  <TableCell><Link to={`/runs/${encodeURIComponent(run.id)}`} className="inline-flex size-8 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground" aria-label={`Open ${run.title}`}><ArrowRight className="size-4" /></Link></TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      ) : null}
    </section>
  )
}

function RunPage() {
  const { runId = "" } = useParams()
  const { refreshKey } = useOutletContext<LayoutContext>()
  const [searchParams, setSearchParams] = useSearchParams()
  const evaluationId = searchParams.get("evaluation")
  const [run, setRun] = useState<ExperimentRun | null>(null)
  const [action, setAction] = useState<"refresh" | "retry" | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [evaluations, setEvaluations] = useState<Evaluation[]>([])
  const [selectedEvaluation, setSelectedEvaluation] = useState<Evaluation | null>(null)
  const [evaluationLoading, setEvaluationLoading] = useState(false)
  const [evaluationAction, setEvaluationAction] = useState(false)
  const [evaluationError, setEvaluationError] = useState<string | null>(null)
  const [evaluationRefresh, setEvaluationRefresh] = useState(0)
  const [pendingEvaluationKey, setPendingEvaluationKey] = useState<{ runId: string; key: string } | null>(null)
  const [retryKeyStorageWarning, setRetryKeyStorageWarning] = useState(
    () => readPendingEvaluationKey(runId).unavailable,
  )

  useEffect(() => {
    let active = true
    setError(null)
    getRun(runId)
      .then((item) => { if (active) { setRun(item); setError(null) } })
      .catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : "Run details could not be loaded.") })
    return () => { active = false }
  }, [runId, refreshKey])

  useEffect(() => {
    let active = true
    if (!run || run.kind !== "simulation") {
      setEvaluations([])
      setSelectedEvaluation(null)
      setEvaluationLoading(false)
      setEvaluationError(null)
      return () => { active = false }
    }
    setEvaluationLoading(true)
    setEvaluationError(null)
    const load = async () => {
      const history = await getEvaluations(runId)
      const selected = evaluationId
        ? await getEvaluation(runId, evaluationId)
        : await getLatestEvaluation(runId)
      return { history, selected }
    }
    load()
      .then(({ history, selected }) => {
        if (active) {
          setEvaluations(history)
          setSelectedEvaluation(selected)
          setEvaluationLoading(false)
        }
      })
      .catch((reason: unknown) => {
        if (active) {
          setEvaluationError(reason instanceof Error ? reason.message : "Evaluation results could not be loaded.")
          setEvaluationLoading(false)
        }
      })
    return () => { active = false }
  }, [run?.kind, runId, evaluationId, refreshKey, evaluationRefresh])

  const perform = async (type: "refresh" | "retry") => {
    setAction(type)
    setError(null)
    try {
      if (type === "refresh") await refreshRun(runId)
      else await retryRun(runId)
      setRun(await getRun(runId))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The action could not be completed.")
    } finally {
      setAction(null)
    }
  }

  const evaluate = async () => {
    setEvaluationAction(true)
    setEvaluationError(null)
    try {
      const currentSession = await getSession()
      const saved = readPendingEvaluationKey(runId)
      if (saved.unavailable) setRetryKeyStorageWarning(true)
      const key = pendingEvaluationKey?.runId === runId
        ? pendingEvaluationKey.key
        : saved.key ?? currentSession.idempotency_key
      setPendingEvaluationKey({ runId, key })
      if (!storePendingEvaluationKey(runId, key)) setRetryKeyStorageWarning(true)
      const result = await startEvaluation(runId, { ...currentSession, idempotency_key: key })
      if (!result.evaluation_id) throw new Error("The evaluation outcome could not be confirmed. Retry to check the same request.")
      setPendingEvaluationKey(null)
      if (!clearPendingEvaluationKey(runId)) setRetryKeyStorageWarning(true)
      setSearchParams({ evaluation: result.evaluation_id })
      setEvaluationRefresh((value) => value + 1)
    } catch (reason) {
      setEvaluationError(reason instanceof Error ? reason.message : "Evaluation could not be started.")
    } finally {
      setEvaluationAction(false)
    }
  }

  if (!run || run.id !== runId) {
    if (error) return <ErrorNotice message={error} title="Run unavailable" />
    return <PageLoading />
  }

  return (
    <section className="space-y-6">
      <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground"><Link to="/runs" className="hover:text-foreground">Runs</Link><span aria-hidden="true">/</span><span className="text-foreground">{run.title}</span></div>
      <div className="flex flex-col gap-4 border-b pb-6 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-2"><h1 className="text-2xl font-semibold tracking-tight">{run.title}</h1><p className="text-sm text-muted-foreground">Run {run.id.slice(0, 8)} · {formatDate(run.created_at)}</p></div>
        <StatusBadge status={run.status} />
      </div>
      {error && <ErrorNotice message={error} title="Could not update this run" />}
      {evaluationError && <ErrorNotice message={evaluationError} title="Evaluation results unavailable" />}
      <Card>
        <CardHeader className="gap-1 sm:flex-row sm:items-center sm:justify-between">
          <div><CardTitle className="text-base">{run.kind === "simulation" ? "Company activity" : "Conversation"}</CardTitle><CardDescription>{run.kind === "simulation" ? "Delivered messages and recorded activity for this simulation." : "Rocket.Chat messages for this task."}</CardDescription></div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => void perform("refresh")} disabled={action !== null}>
              {action === "refresh" ? <LoaderCircle className="animate-spin" /> : <RefreshCw />}
              {run.kind === "simulation" ? "Refresh activity" : "Sync messages"}
            </Button>
            {run.status === "failed" || run.status === "uncertain" ? (
              <Button onClick={() => void perform("retry")} disabled={action !== null}>
                {action === "retry" ? <LoaderCircle className="animate-spin" /> : <ArrowUpRight />}
                {run.kind === "simulation" ? "Retry initialization" : "Retry send"}
              </Button>
            ) : null}
          </div>
        </CardHeader>
        {run.error && (
          <CardContent className="pt-0"><ErrorNotice message={run.error} title="Run detail" /></CardContent>
        )}
        {run.chat_url && (
          <CardContent className="pt-0">
            <a href={run.chat_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1.5 text-sm text-primary underline-offset-4 hover:underline">Open in Rocket.Chat <ExternalLink className="size-3.5" /></a>
          </CardContent>
        )}
        {run.provider_url && (
          <CardContent className="pt-0">
            <a href={run.provider_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1.5 text-sm text-primary underline-offset-4 hover:underline">Open provider case <ExternalLink className="size-3.5" /></a>
            <p className="mt-2 text-xs text-muted-foreground">Use the responsible employee’s company email login. Case records and correspondence stay in the company.</p>
          </CardContent>
        )}
        <Separator />
        <CardContent className="space-y-4 py-5">
          {run.kind === "simulation" && <div className="space-y-3">
            {run.simulated_time && <p className="text-sm text-muted-foreground">Company time: {formatDate(run.simulated_time)}</p>}
            {run.activity?.map((item) => <article key={item.id} className="rounded-lg border p-4"><p className="text-sm font-medium">{item.description}</p><time className="text-xs text-muted-foreground">{formatDate(item.timestamp)}</time></article>)}
          </div>}
          {run.messages.length > 0 ? run.messages.map((message, index) => (
            <article key={`${message.timestamp}-${message.author}-${index}`} className="rounded-lg border bg-background p-4">
              <header className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-sm font-medium"><span className="grid size-7 place-items-center rounded-full bg-secondary text-secondary-foreground"><MessageCircle className="size-3.5" /></span>{message.author}</div>
                <time className="text-xs text-muted-foreground">{message.timestamp ? formatDate(message.timestamp) : ""}</time>
              </header>
              <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6">{message.text}</p>
            </article>
          )) : (
            <div className="rounded-lg border border-dashed px-5 py-10 text-center">
              <MessageCircle className="mx-auto size-7 text-muted-foreground" />
              <p className="mt-3 text-sm font-medium">No messages yet</p>
              <p className="mt-1 text-sm text-muted-foreground">Refresh to check for new activity.</p>
            </div>
          )}
        </CardContent>
      </Card>
      <ResultsPanel
          available={run.kind === "simulation"}
          runId={runId}
          evaluations={evaluations}
          selected={selectedEvaluation}
          selectedId={evaluationId}
          loading={evaluationLoading}
          evaluating={evaluationAction}
          retryingRequest={
            (pendingEvaluationKey?.runId === runId && evaluationError !== null)
            || readPendingEvaluationKey(runId).key !== null
          }
          onEvaluate={() => void evaluate()}
          onRefresh={() => setEvaluationRefresh((value) => value + 1)}
        />
      {retryKeyStorageWarning && run.kind === "simulation" && (
        <p className="text-xs text-amber-700">This browser could not persist the retry key. Keep this page open until the evaluation outcome is confirmed.</p>
      )}
      <Button variant="ghost" render={<Link to="/runs" />}><ArrowLeft /> Back to runs</Button>
    </section>
  )
}

function ResultsPanel({
  available, runId, evaluations, selected, selectedId, loading, evaluating, retryingRequest, onEvaluate, onRefresh,
}: {
  available: boolean
  runId: string
  evaluations: Evaluation[]
  selected: Evaluation | null
  selectedId: string | null
  loading: boolean
  evaluating: boolean
  retryingRequest: boolean
  onEvaluate: () => void
  onRefresh: () => void
}) {
  return (
    <section className="space-y-4" aria-labelledby="results-heading">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h2 id="results-heading" className="text-lg font-semibold">Results</h2><p className="text-sm text-muted-foreground">Native benchmark scores and per-criterion results.</p></div>
        <div className="flex gap-2">
          {available && <>
            <Button variant="outline" onClick={onRefresh} disabled={loading || evaluating}><RefreshCw /> Refresh results</Button>
            <Button onClick={onEvaluate} disabled={evaluating}>{evaluating ? <LoaderCircle className="animate-spin" /> : <Beaker />} {retryingRequest ? "Retry evaluation request" : "Evaluate run"}</Button>
          </>}
        </div>
      </div>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(16rem,1fr)]">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">{selected ? `Evaluation ${selected.evaluation_id.slice(0, 8)}` : "Latest evaluation"}</CardTitle>
            <CardDescription>{selected?.evaluated_at ? `Evaluated ${formatDate(selected.evaluated_at)}` : selected?.requested_at ? `Requested ${formatDate(selected.requested_at)}` : "A new report will appear here after evaluation."}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            {loading ? <Skeleton className="h-28 w-full" /> : selected ? <EvaluationReport evaluation={selected} /> : (
              <div className="rounded-lg border border-dashed px-5 py-10 text-center">
                <p className="font-medium">Not evaluated</p>
                <p className="mt-1 text-sm text-muted-foreground">{available ? "No benchmark report is recorded for this run yet." : "This workplace run has no benchmark evaluator, so no score report is available."}</p>
              </div>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle className="text-base">Evaluation history</CardTitle><CardDescription>Each report is stored separately.</CardDescription></CardHeader>
          <CardContent className="space-y-2">
            {!available ? <p className="text-sm text-muted-foreground">Not available for this run.</p> : evaluations.length === 0 ? <p className="text-sm text-muted-foreground">No reports yet.</p> : evaluations.map((item) => (
              <Link
                key={item.evaluation_id}
                to={`/runs/${encodeURIComponent(runId)}?evaluation=${encodeURIComponent(item.evaluation_id)}`}
                className={`block rounded-lg border p-3 hover:bg-muted/50 ${selectedId === item.evaluation_id ? "border-primary bg-muted/40" : ""}`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm font-medium">{item.benchmark ?? "Benchmark"} · {evaluationLabel(item)}</span>
                  <ArrowRight className="size-4 shrink-0 text-muted-foreground" />
                </div>
                <span className="mt-1 block text-xs text-muted-foreground">{item.evaluated_at ? formatDate(item.evaluated_at) : item.requested_at ? `Requested ${formatDate(item.requested_at)}` : item.lifecycle ?? item.evaluation_id.slice(0, 8)}</span>
              </Link>
            ))}
          </CardContent>
        </Card>
      </div>
    </section>
  )
}

function evaluationLabel(evaluation: Evaluation): string {
  if (evaluation.lifecycle === "reserved") return "Queued"
  if (evaluation.lifecycle === "running") return "Running"
  if (evaluation.lifecycle === "uncertain") return "Outcome uncertain"
  if (evaluation.status === "failed") return "Evaluation failed"
  if (evaluation.status === "incomplete") return "Incomplete"
  return "Scored"
}

function EvaluationReport({ evaluation }: { evaluation: Evaluation }) {
  const pending = evaluation.lifecycle === "reserved" || evaluation.lifecycle === "running" || evaluation.lifecycle === "uncertain"
  const scored = !pending && evaluation.status !== "failed"
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline">{evaluationLabel(evaluation)}</Badge>
        <Badge variant="secondary">{evaluationModeLabel(evaluation.evaluation_mode)}</Badge>
        {evaluation.benchmark && <span className="text-sm text-muted-foreground">{evaluation.benchmark}{evaluation.benchmark_version ? ` ${evaluation.benchmark_version}` : ""}</span>}
      </div>
      {pending ? <p className="text-sm text-muted-foreground">This evaluation has not produced a final report yet. Refresh to check its status.</p> : null}
      {evaluation.status === "failed" && !pending ? (
        <p className="text-sm text-destructive">No score was produced. {evaluation.failure?.code ? `Failure: ${evaluation.failure.code}.` : "The evaluation failed."}</p>
      ) : null}
      {scored && evaluation.scores && Object.keys(evaluation.scores).length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2">
          {Object.entries(evaluation.scores).map(([name, value]) => (
            <div key={name} className="rounded-lg border p-3">
              <p className="text-xs text-muted-foreground">{name.replaceAll("_", " ")}</p>
              <p className="mt-1 text-lg font-semibold tabular-nums">{formatEvaluationValue(value)}</p>
              {evaluation.native_scale?.[name] && <p className="mt-1 text-xs text-muted-foreground">Native scale: {nativeScaleLabel(evaluation.native_scale[name])}</p>}
            </div>
          ))}
        </div>
      )}
      {scored && (evaluation.criterion_results?.length ?? 0) > 0 && (
        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableHeader><TableRow><TableHead>Criterion</TableHead><TableHead>Status</TableHead><TableHead>Value</TableHead><TableHead>Expected</TableHead><TableHead>Gap</TableHead></TableRow></TableHeader>
            <TableBody>{evaluation.criterion_results?.map((criterion, index) => (
              <TableRow key={criterion.criterion_id ?? `${criterion.name ?? criterion.criterion ?? "criterion"}-${index}`}>
                <TableCell className="font-medium">{criterion.name ?? criterion.criterion ?? criterion.criterion_id ?? `Criterion ${index + 1}`}</TableCell>
                <TableCell>{criterion.passed === undefined ? criterion.status ?? "—" : criterion.passed ? "Met" : "Not met"}</TableCell>
                <TableCell>{formatEvaluationValue(criterion.actual ?? criterion.value ?? criterion.score)}</TableCell>
                <TableCell>{formatEvaluationValue(criterion.expected ?? criterion.maximum ?? criterion.max_score)}</TableCell>
                <TableCell>{criterion.gap_code ?? "—"}</TableCell>
              </TableRow>
            ))}</TableBody>
          </Table>
        </div>
      )}
      {!pending && evaluation.status === "incomplete" && <p className="text-sm text-muted-foreground">Some criteria could not be scored. Recorded values are shown above; no combined score is calculated.</p>}
      {scored && evaluation.status === "completed" && (!evaluation.scores || Object.keys(evaluation.scores).length === 0) && <p className="text-sm text-muted-foreground">See criteria below. This evaluation has no single numeric score.</p>}
      {evaluation.mapping_gaps && evaluation.mapping_gaps.length > 0 && <div><h3 className="text-sm font-semibold">Mapping gaps</h3><ul className="mt-1 list-disc pl-5 text-sm text-muted-foreground">{evaluation.mapping_gaps.map((gap) => <li key={gap}>{gap.replaceAll("_", " ")}</li>)}</ul></div>}
      {evaluation.warnings && evaluation.warnings.length > 0 && <div><h3 className="text-sm font-semibold">Warnings</h3><ul className="mt-1 list-disc pl-5 text-sm text-muted-foreground">{evaluation.warnings.map((warning) => <li key={warning}>{warning.replaceAll("_", " ")}</li>)}</ul></div>}
      <p className="text-xs text-muted-foreground">Evaluation mode: {evaluationModeLabel(evaluation.evaluation_mode).toLowerCase()}. Scores retain their individual benchmark scales.</p>
    </div>
  )
}

function CompanyEvaluationEntry({ evaluation, latest }: { evaluation: CompanyNativeEvaluation; latest: boolean }) {
  const scaleLabel = (scale: string | undefined) => scale === "native_metric" ? "native metric" : scale === "native_reward_0_to_1" ? "native reward (0 to 1)" : scale ?? "native scale"
  return <article className="rounded-md bg-muted/40 p-3">
    <div className="flex flex-wrap items-center gap-2">
      <Badge variant={evaluation.status === "completed" ? "secondary" : "outline"}>{evaluation.status}</Badge>
      {latest && <Badge variant="outline">Latest saved</Badge>}
      <span className="text-xs text-muted-foreground">{evaluation.evaluated_at ? formatDate(evaluation.evaluated_at) : evaluation.requested_at ? `Requested ${formatDate(evaluation.requested_at)}` : "Evaluation time unavailable"}</span>
    </div>
    <p className="mt-2 text-xs text-muted-foreground">Source seed {evaluation.seed ?? "—"} · started {evaluation.started_at ? formatDate(evaluation.started_at) : "—"} · company time {evaluation.native_company_time ? formatDate(evaluation.native_company_time) : "—"}</p>
    {evaluation.upstream_commit && <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">Upstream {evaluation.upstream_commit}</p>}
    {evaluation.evidence_hash && <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">Evidence SHA-256 {evaluation.evidence_hash}</p>}
    {evaluation.native_state_hash && <p className="mt-1 break-all font-mono text-[11px] text-muted-foreground">Native state SHA-256 {evaluation.native_state_hash}</p>}
    {evaluation.status === "failed" && <p className="mt-2 text-sm text-destructive">No score was produced.{evaluation.failure?.code ? ` ${evaluation.failure.code}.` : ""}</p>}
    {evaluation.status !== "failed" && Object.keys(evaluation.scores ?? {}).length > 0 && <dl className="mt-3 grid gap-2 sm:grid-cols-2">
      {Object.entries(evaluation.scores ?? {}).map(([name, value]) => <div key={name} className="rounded bg-background px-2.5 py-2">
        <dt className="text-xs text-muted-foreground">{name.replaceAll(".", " · ")} · {scaleLabel(evaluation.native_scale?.[name])}{evaluation.money_metric_units?.[name] ? ` (${evaluation.money_metric_units[name]})` : ""}</dt>
        <dd className="mt-1 break-words font-semibold tabular-nums">{formatEvaluationValue(value)}</dd>
      </div>)}
    </dl>}
    {evaluation.status !== "failed" && evaluation.criterion_results?.length ? <div className="mt-3 space-y-1.5">
      {evaluation.criterion_results.map((criterion, index) => <div key={`${criterion.criterion_id ?? criterion.name ?? "criterion"}-${index}`} className="flex flex-wrap justify-between gap-x-3 text-sm">
        <span>{criterion.name ?? criterion.criterion ?? criterion.criterion_id ?? "Criterion"}</span>
        <span className="tabular-nums text-muted-foreground">{formatEvaluationValue(criterion.actual ?? criterion.value ?? criterion.score)}{criterion.status ? ` · ${criterion.status}` : ""}</span>
      </div>)}
    </div> : null}
    {evaluation.status === "incomplete" && <p className="mt-2 text-xs text-muted-foreground">Partial native result; missing values remain unscored. No combined score is calculated.</p>}
    {!!evaluation.mapping_gaps?.length && <p className="mt-2 text-xs text-muted-foreground">Mapping gaps: {evaluation.mapping_gaps.join(", ")}</p>}
    {!!evaluation.warnings?.length && <p className="mt-1 text-xs text-muted-foreground">Warnings: {evaluation.warnings.join(", ")}</p>}
  </article>
}

function formatEvaluationValue(value: number | string | boolean | null | undefined): string {
  return value === null || value === undefined ? "—" : String(value)
}

function nativeScaleLabel(scale: NonNullable<Evaluation["native_scale"]>[string]): string {
  return ({
    boolean: "boolean",
    categorical: "categorical",
    seconds: "seconds",
    native_reward_0_to_1: "native reward, 0 to 1",
    count: "count",
    native_metric: "native metric",
  } as const)[scale]
}

function evaluationModeLabel(mode: Evaluation["evaluation_mode"]): string {
  if (mode === "company_adapted") return "Company adapted"
  if (mode === "native_benchmark") return "Native benchmark"
  return "Mode unavailable"
}

function pendingEvaluationStorageKey(runId: string): string {
  return `experiments.pending-evaluation.${runId}`
}

function readPendingEvaluationKey(runId: string): { key: string | null; unavailable: boolean } {
  try {
    return { key: window.sessionStorage.getItem(pendingEvaluationStorageKey(runId)), unavailable: false }
  } catch {
    return { key: null, unavailable: true }
  }
}

function storePendingEvaluationKey(runId: string, key: string): boolean {
  try {
    window.sessionStorage.setItem(pendingEvaluationStorageKey(runId), key)
    return true
  } catch {
    return false
  }
}

function clearPendingEvaluationKey(runId: string): boolean {
  try {
    window.sessionStorage.removeItem(pendingEvaluationStorageKey(runId))
    return true
  } catch {
    return false
  }
}

function PageLoading() {
  return <div className="space-y-5"><Skeleton className="h-8 w-56" /><Skeleton className="h-4 w-96 max-w-full" /><Skeleton className="h-64 w-full" /></div>
}

function NotFound() {
  return (
    <div className="mx-auto max-w-lg space-y-3 py-16 text-center">
      <h1 className="text-xl font-semibold">Page not found</h1>
      <p className="text-sm text-muted-foreground">This task or run may no longer be available.</p>
      <Button render={<Link to="/" />}>Back to activity</Button>
    </div>
  )
}

function formatActivityText(value: string): string {
  return value.replace(/\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b/g, formatDate)
}

function formatDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date)
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={<ActivityPage />} />
          <Route path="streams" element={<EmployeeStreamsPage />} />
          <Route path="experiments" element={<LibraryPage />} />
          <Route path="experiments/background" element={<section className="space-y-5">
            <h1 className="text-2xl font-semibold tracking-tight">Background simulations</h1>
            <BackgroundSimulations />
          </section>} />
          <Route path="experiments/coffeebench/settings" element={<BenchmarkSettingsPage key="coffee" />} />
          <Route path="experiments/ecommercebench/settings" element={<BenchmarkSettingsPage key="ecommerce" ecommerce />} />
          <Route path="experiments/:experimentId" element={<ExperimentPage />} />
          <Route path="settings/company-clock" element={<CompanyClockPage />} />
          <Route path="help" element={<HelpPage />} />
          <Route path="help/company-environment" element={<HelpSection title="Company environment"><CompanySwitcher /></HelpSection>} />
          <Route path="help/context" element={<HelpSection title="Codex connection"><CodexConnectionCard /></HelpSection>} />
          <Route path="help/native-evaluations" element={<HelpSection title="Native evaluations"><SavedNativeEvaluations /></HelpSection>} />
          <Route path="help/company-tools" element={<CompanyToolsPage />} />
          <Route path="runs" element={<RunsPage />} />
          <Route path="runs/:runId" element={<RunPage />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
