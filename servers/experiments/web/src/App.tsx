import { useEffect, useMemo, useRef, useState } from "react"
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
  getCatalog,
  getCompatibilityCatalogue,
  getCompany,
  getCompanyEvaluations,
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
  type ActivityEntry,
  type ActivityMetrics,
  type Company,
  type CompanyNativeEvaluation,
  type CompanyNativeEvaluationProvider,
  type CompatibilityCatalogue,
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

type LayoutContext = { refreshKey: number; liveError: string | null }

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

function AppLayout() {
  const [refreshKey, setRefreshKey] = useState(0)
  const [liveError, setLiveError] = useState<string | null>(null)
  const snapshot = useRef<Map<string, string> | undefined>(undefined)

  useEffect(() => {
    const source = new EventSource("/api/events")
    source.onopen = () => setLiveError(null)
    source.onerror = () => setLiveError("Live updates are reconnecting. Open a run and use Sync messages to check for replies.")
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
        setLiveError("Live updates could not be read. Open a run and use Sync messages to check for replies.")
      }
    })
    source.addEventListener("activity", () => setRefreshKey((current) => current + 1))
    source.addEventListener("company", () => setRefreshKey((current) => current + 1))
    return () => source.close()
  }, [])

  return (
    <div className="min-h-svh bg-background text-foreground">
      <header className="sticky top-0 z-20 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-4 sm:px-6">
          <Link to="/" className="flex items-center gap-2.5 font-semibold tracking-tight" aria-label="The Agentic Company activity">
            <span className="grid size-8 place-items-center rounded-lg bg-primary text-primary-foreground"><Beaker className="size-4" /></span>
            <span>The Agentic Company</span>
          </Link>
          <nav className="flex items-center gap-1" aria-label="Main navigation">
            <NavLink to="/" end className={({ isActive }) => `rounded-md px-3 py-2 text-sm transition-colors ${isActive ? "bg-secondary font-medium text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}>
              Activity
            </NavLink>
          <NavLink to="/experiments" className={({ isActive }) => `rounded-md px-3 py-2 text-sm transition-colors ${isActive ? "bg-secondary font-medium text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}>
              Experiments
            </NavLink>
            <NavLink to="/coverage" className={({ isActive }) => `rounded-md px-3 py-2 text-sm transition-colors ${isActive ? "bg-secondary font-medium text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}>
              Coverage
            </NavLink>
            <NavLink to="/runs" className={({ isActive }) => `rounded-md px-3 py-2 text-sm transition-colors ${isActive ? "bg-secondary font-medium text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}>
              Runs
            </NavLink>
            <NavLink to="/help" className={({ isActive }) => `rounded-md px-3 py-2 text-sm transition-colors ${isActive ? "bg-secondary font-medium text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}>
              Help
            </NavLink>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6 sm:py-10">
        {liveError && (
          <Alert className="mb-6">
            <CircleAlert />
            <AlertTitle>Live updates unavailable</AlertTitle>
            <AlertDescription>{liveError}</AlertDescription>
          </Alert>
        )}
        <Outlet context={{ refreshKey, liveError } satisfies LayoutContext} />
      </main>
    </div>
  )
}

function useRefreshKey() {
  return useOutletContext<LayoutContext>().refreshKey
}

function companyProviderStatusLabel(company: Company | null, providerId: string) {
  const errors = company?.background?.errors
  const issue = errors?.[providerId] ?? errors?.configuration
  const provider = company?.background?.providers[providerId]
  if (provider?.enabled === false && (provider.status === "stopping" || provider.in_flight)) return "Stopping"
  if (provider?.enabled === false) return `Stopped${issue ? ` (${issue.code})` : ""}`
  if (issue) {
    const uncertain = /uncertain|pending/i.test(issue.code)
    return `${uncertain ? "Outcome uncertain" : "Needs attention"} (${issue.code})`
  }
  if (provider?.status === "error") return "Needs attention"
  return provider?.status ?? (company?.background?.initialized ? "Scheduled" : "Connecting")
}

function bankingRoleLabel(roleFit?: string) {
  if (roleFit === "personal_customer") return "Personal banking"
  if (roleFit === "mixed_customer") return "Personal + business"
  if (roleFit === "business_customer") return "Business banking"
  return null
}

function ActivityPage() {
  const refreshKey = useRefreshKey()
  const [company, setCompany] = useState<Company | null>(null)
  const [activity, setActivity] = useState<ActivityEntry[]>([])
  const [metrics, setMetrics] = useState<ActivityMetrics | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activityWarning, setActivityWarning] = useState<string | null>(null)
  const [manualRefresh, setManualRefresh] = useState(0)
  const [refreshing, setRefreshing] = useState(false)
  const [filter, setFilter] = useState("all")
  const [nativeEvaluations, setNativeEvaluations] = useState<Record<string, CompanyNativeEvaluationProvider> | null>(null)
  const [nativeEvaluationError, setNativeEvaluationError] = useState<string | null>(null)
  const [evaluatingProvider, setEvaluatingProvider] = useState<string | null>(null)
  const [backgroundControlBusy, setBackgroundControlBusy] = useState<Record<string, boolean>>({})
  const [backgroundControlErrors, setBackgroundControlErrors] = useState<Record<string, string>>({})

  useEffect(() => {
    let active = true
    Promise.allSettled([getCompany(), getActivity(), getCompanyEvaluations()])
      .then(([companyResult, activityResult, evaluationsResult]) => {
        if (!active) return
        const errors: string[] = []
        if (companyResult.status === "fulfilled") setCompany(companyResult.value)
        else errors.push(companyResult.reason instanceof Error ? companyResult.reason.message : "Company status could not be loaded.")
        if (activityResult.status === "fulfilled") {
          setActivity(activityResult.value.activity)
          setMetrics(activityResult.value.metrics)
          setActivityWarning(activityResult.value.error ?? null)
        } else {
          setMetrics(null)
          setActivityWarning(activityResult.reason instanceof Error ? activityResult.reason.message : "Company activity could not be loaded.")
        }
        if (evaluationsResult.status === "fulfilled") {
          setNativeEvaluations(evaluationsResult.value)
          setNativeEvaluationError(null)
        } else {
          setNativeEvaluationError(evaluationsResult.reason instanceof Error ? evaluationsResult.reason.message : "Saved native evaluations could not be loaded.")
        }
        setError(errors.length ? errors.join(" ") : null)
      })
      .finally(() => {
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

  const setBackgroundProvider = async (providerId: "coffeebench" | "ecommercebench", enabled: boolean) => {
    setBackgroundControlBusy((current) => ({ ...current, [providerId]: true }))
    setBackgroundControlErrors((current) => ({ ...current, [providerId]: "" }))
    try {
      await setBackgroundProviderEnabled(providerId, enabled)
      setCompany(await getCompany())
    } catch (reason) {
      setBackgroundControlErrors((current) => ({ ...current, [providerId]: reason instanceof Error ? reason.message : `${providerId} control could not be confirmed. Refresh to reconcile the requested state.` }))
    } finally {
      setBackgroundControlBusy((current) => ({ ...current, [providerId]: false }))
    }
  }

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

  const filtered = [...activity]
    .filter((entry) => filter === "all" || entry.type === filter || (filter === "message" && entry.type === "chat") || (filter === "chatter" && (entry.type === "chatter" || (entry.type === "message" && entry.direction === "sent" && Boolean(entry.actor)))))
    .sort((first, second) => second.timestamp.localeCompare(first.timestamp))
  const running = company?.status === "running"
  const backgroundErrors = company?.background?.errors ?? {}
  const globalBackgroundErrors = ([
    ["company_background", "Background worker"],
    ["company_feedback_reconcile", "Company feedback reconciliation"],
    ["native_finance_reconcile", "Finance synchronization"],
  ] as const).flatMap(([code, label]) => backgroundErrors[code]
    ? [{ code, label, detail: backgroundErrors[code].code }]
    : [])
  const companyStatus = !company
    ? loading ? "Loading status…" : "Status unavailable"
    : company.status === "starting" ? "Starting"
    : company.status === "running" ? "Company service running"
    : company.status === "paused" ? "Paused"
    : company.status === "stopped" ? "Stopped"
    : "Status unavailable"

  return (
    <section className="space-y-7">
      <div className="flex flex-col gap-2 border-b pb-6 sm:flex-row sm:items-end sm:justify-between">
        <div className="space-y-1.5">
          <h1 className="text-2xl font-semibold tracking-tight">Company activity</h1>
          <p className="text-sm text-muted-foreground">See what your company is sending, receiving and doing.</p>
        </div>
        <Button variant="outline" render={<Link to="/experiments" />}>Browse experiments <ArrowRight /></Button>
      </div>

      <Card>
        <CardContent className="flex flex-col gap-4 py-5 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-3">
            <span className="grid size-11 place-items-center rounded-xl bg-secondary text-secondary-foreground"><Building2 className="size-5" /></span>
            <div>
              <p className="font-medium">{company?.name ?? "Your company"}</p>
              <p className="mt-1 flex items-center gap-2 text-sm text-muted-foreground">
                <span className={`size-2 rounded-full ${running ? "bg-emerald-500" : "bg-muted-foreground"}`} />
                {companyStatus}
                {company?.simulated_time && <span>· Company time {formatDate(company.simulated_time)}</span>}
              </p>
              {company?.background && <div className="mt-2 flex flex-wrap gap-2">
                {([["coffeebench", "CoffeeBench"], ["ecommercebench", "E-CommerceBench"]] as const).map(([id, label]) => {
                  const error = company.background?.errors[id] ?? company.background?.errors.configuration
                  const provider = company.background?.providers[id]
                  const status = companyProviderStatusLabel(company, id)
                  return <Badge key={id} variant={error || provider?.status === "error" ? "destructive" : "outline"}
                    aria-label={`${label} status: ${status}`}
                    title={provider?.next_due ? `Next company-time update: ${formatDate(provider.next_due)}` : undefined}>
                    {label}: {status}
                  </Badge>
                })}
                <p className="w-full text-xs text-muted-foreground">Company availability is separate from background provider health.</p>
              </div>}
              {company?.background && <div className="mt-3 space-y-3">
                {([['coffeebench', 'CoffeeBench'], ['ecommercebench', 'E-CommerceBench']] as const).map(([providerId, label]) => {
                  const provider = company.background?.providers[providerId]
                  const enabled = provider?.enabled
                  return <div key={providerId} className="flex flex-wrap items-center gap-3">
                    <Button size="sm" variant="outline" disabled={backgroundControlBusy[providerId] || enabled === undefined || provider?.status === "stopping" || (enabled === false && provider?.in_flight === true)}
                      onClick={() => void setBackgroundProvider(providerId, !enabled)}>
                      {backgroundControlBusy[providerId] ? <LoaderCircle className="animate-spin" /> : null}
                      {enabled ? `Stop ${label}` : `Start ${label}`}
                    </Button>
                    {enabled === false && (provider?.status === "stopping" || provider?.in_flight)
                      ? <span className="text-sm text-muted-foreground">Finishing current work. Company data is preserved.</span>
                      : enabled === false && <span className="text-sm text-muted-foreground">{label} stopped. Company data is preserved.</span>}
                    {enabled === true && provider?.status === "starting" && <span className="text-sm text-muted-foreground">{label} is starting…</span>}
                    {backgroundControlErrors[providerId] && <span className="text-sm text-destructive">{backgroundControlErrors[providerId]}</span>}
                  </div>
                })}
              </div>}
            </div>
          </div>

        </CardContent>
      </Card>

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
      {error && <ErrorNotice message={error} title="Company activity unavailable" />}

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
          <div className="space-y-1"><h2 className="text-lg font-semibold">Live activity</h2><p className="text-sm text-muted-foreground">Emails, messages and completed company actions appear here as they happen.</p></div>
          <Button variant="outline" size="sm" disabled={refreshing} aria-label="Refresh company activity" onClick={refreshCompanyActivity}>
            <RefreshCw className={refreshing ? "animate-spin" : ""} /> Refresh activity
          </Button>
        </div>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Filter activity">
          {[ ["all", "All activity"], ["email", "Emails"], ["message", "Messages"], ["chatter", "Chatter"], ["action", "Company actions"] ].map(([value, label]) => (
            <Button key={value} size="sm" variant={filter === value ? "default" : "outline"} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</Button>
          ))}
        </div>
      </div>

      {activityWarning && <ErrorNotice message={activityWarning} title="Some company activity could not be loaded" />}
      {loading ? <div className="space-y-3"><Skeleton className="h-24 w-full" /><Skeleton className="h-24 w-full" /></div> : filtered.length ? (
        <div className="space-y-3">
          {filtered.map((entry) => {
            const isEmail = entry.type === "email"
            const Icon = isEmail ? Mail : entry.type === "message" || entry.type === "chat" ? MessageCircle : Building2
            return <article key={entry.id} className="rounded-xl border bg-card p-4 sm:p-5">
              <div className="flex items-start gap-3">
                <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-secondary text-secondary-foreground"><Icon className="size-4" /></span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                    <h3 className="font-medium">{entry.title}</h3>
                    <time className="text-xs text-muted-foreground">{formatDate(entry.timestamp)}</time>
                  </div>
                  {(entry.actor || entry.direction) && <p className="mt-1 text-xs text-muted-foreground">{[entry.actor, entry.direction === "sent" ? "Sent" : entry.direction === "received" ? "Received" : null].filter(Boolean).join(" · ")}</p>}
                  {entry.summary && <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-foreground/85">{entry.summary}</p>}
                  {entry.content && <p className="mt-3 whitespace-pre-wrap break-words rounded-lg bg-muted/60 p-3 text-sm leading-6">{entry.content}</p>}
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

function HelpPage() {
  return (
    <section className="space-y-7">
      <h1 className="text-2xl font-semibold tracking-tight">Help</h1>
      <Card>
        <CardHeader>
          <CardTitle>Company tools</CardTitle>
          <CardDescription>Open the company’s systems and administration.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          {[
            ["ERPNext", "https://erp.company.example.test"],
            ["Email", "https://mail.company.example.test"],
            ["Rocket.Chat", "https://chat.company.example.test"],
            ["ownCloud", "https://owncloud.company.example.test"],
            ["Plane", "https://plane.company.example.test"],
            ["GitLab", "https://gitlab.company.example.test"],
            ["midPoint", "https://midpoint.company.example.test/midpoint/"],
          ].map(([name, href]) => (
            <Button key={name} variant="outline" render={<a href={href} target="_blank" rel="noopener noreferrer" />}>
              {name}<ExternalLink className="size-4" />
            </Button>
          ))}
        </CardContent>
      </Card>
    </section>
  )
}

function LibraryPage() {
  const refreshKey = useRefreshKey()
  const [experiments, setExperiments] = useState<Experiment[]>([])
  const [providerWarning, setProviderWarning] = useState<string | null>(null)
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
      .then(({ experiments: items, provider_catalog_error }) => {
        if (active) {
          setExperiments(items)
          setProviderWarning(provider_catalog_error
            ? `The travel, banking and telecom catalogue could not be refreshed (${provider_catalog_error.code}).${provider_catalog_error.correlation_id ? ` Correlation ID: ${provider_catalog_error.correlation_id}.` : ""} Refresh to retry.`
            : null)
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
  }, [refreshKey])

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
          <p className="text-sm text-muted-foreground">Choose a company problem. Filter by type or benchmark.</p>
        </div>
        <div className="flex w-full items-center gap-2 sm:w-80">
          <div className="relative w-full">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
            <Input value={search} onChange={(event) => updateFilters(event.target.value, selectedTypes)} placeholder="Search tasks" className="h-10 pl-9" aria-label="Search experiments" />
          </div>
        </div>
      </div>

      {providerWarning && <ErrorNotice title="Provider catalogue unavailable" message={providerWarning} />}
      <Card>
        <CardContent className="flex flex-col gap-3 pt-6 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="font-medium">Know what fits this company</p>
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
              Customer requests and external provider cases run in this company. The coverage guide explains source gaps, business fit and the background economies.
            </p>
          </div>
          <Button variant="outline" render={<Link to="/coverage" />}>View coverage <ArrowRight /></Button>
        </CardContent>
      </Card>

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
                      {experiment.provider_id === "tau2-banking" && bankingRoleLabel(experiment.role_fit) &&
                        <Badge variant="outline" title={experiment.role_fit_note}>{bankingRoleLabel(experiment.role_fit)}</Badge>}
                      {experiment.task_id && <Badge variant="outline">τ-bench #{experiment.task_id}</Badge>}
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

function CoveragePage() {
  const [catalogue, setCatalogue] = useState<CompatibilityCatalogue | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let active = true
    getCompatibilityCatalogue().then((value) => {
      if (active) { setCatalogue(value); setError(null) }
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "Compatibility coverage could not be loaded.")
    })
    return () => { active = false }
  }, [])

  if (error) return <ErrorNotice message={error} title="Coverage unavailable" />
  if (!catalogue) return <PageLoading />

  const fitLabel: Record<string, string> = {
    direct_company_adapted: "Adapted to this company",
    provider_persona_for_unchanged_tasks: "Original tasks require the provider role",
    mixed_provider_and_external_financial_work: "Mixed provider and possible company finance work",
    potential_external_company_financial_interaction: "Potential company-bank interaction; task-level review needed",
    potential_external_financial_work_for_bill_affecting_tasks: "Bill-affecting tasks may fit an authorized company carrier account",
    requires_service_persona: "Requires a service-business persona",
    internal_test_fixtures: "Internal test fixtures, not assignments",
  }

  return (
    <section className="space-y-7">
      <div className="flex flex-col gap-3 border-b pb-6 sm:flex-row sm:items-end sm:justify-between">
        <div className="space-y-1.5">
          <h1 className="text-2xl font-semibold tracking-tight">Company fit and catalogue coverage</h1>
          <p className="max-w-3xl text-sm text-muted-foreground">{catalogue.company_persona}</p>
        </div>
        <Button variant="outline" render={<Link to="/experiments" />}>Browse current library <ArrowRight /></Button>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {catalogue.library_inventory.map((item) => (
          <Card key={item.name}>
            <CardHeader>
              <div className="flex items-baseline justify-between gap-3">
                <CardTitle className="text-base">{item.name}</CardTitle>
                <Badge variant="secondary">{item.count.toLocaleString()}</Badge>
              </div>
              <CardDescription className="leading-relaxed">{item.summary}</CardDescription>
            </CardHeader>
          </Card>
        ))}
      </div>

      <div className="space-y-3">
        <div>
          <h2 className="text-lg font-semibold">τ-bench domain inventory</h2>
          <p className="text-sm text-muted-foreground">Counts describe source catalogue rows. They are not run or pass counts.</p>
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          {catalogue.tau_domains.map((domain) => (
            <Card key={domain.name}>
              <CardHeader className="gap-3">
                <div className="flex flex-wrap items-baseline justify-between gap-3">
                  <CardTitle className="text-base">{domain.name}</CardTitle>
                  <div className="flex gap-2 text-xs text-muted-foreground">
                    <Badge variant="outline">{domain.catalogue_count.toLocaleString()} source rows</Badge>
                    <Badge variant="outline">{domain.default_base_count.toLocaleString()} default-base rows</Badge>
                  </div>
                </div>
                <Badge variant={domain.fit === "direct_company_adapted" ? "secondary" : "outline"} className="w-fit">
                  {fitLabel[domain.fit] ?? "Requires review"}
                </Badge>
                <CardDescription className="leading-relaxed">{domain.summary}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                <p><span className="font-medium">Implementation:</span> {domain.implementation}</p>
                {domain.source_integrity_issue_count !== undefined && domain.source_integrity_issue_count > 0 && (
                  <p className="rounded-md bg-muted p-3 text-muted-foreground">
                    {domain.source_integrity_issue_count} upstream source-reference issues affect some retail task rows. They remain in the library and are not filled with invented records.
                  </p>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Source revisions</CardTitle>
          <CardDescription>These revisions identify the upstream catalogues summarized here.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {catalogue.source_revisions.map((source) => (
            <p key={source.name} className="flex flex-wrap justify-between gap-x-4 gap-y-1">
              <span>{source.name}</span><code className="text-xs text-muted-foreground">{source.revision}</code>
            </p>
          ))}
        </CardContent>
      </Card>
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
      .then(([{ experiments: items, provider_catalog_error }, currentSession]) => {
        if (!active) return
        const selected = items.find((item) => item.id === experimentId) ?? null
        if (!selected && provider_catalog_error && experimentId.startsWith("tau-provider-")) {
          throw new Error(`The provider task catalogue is unavailable (${provider_catalog_error.code}). Refresh to retry.`)
        }
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
        {(experiment.role_fit_note || experiment.role_fit) && <p className="text-sm text-muted-foreground">{experiment.role_fit_note || `Source context: ${experiment.role_fit?.replaceAll("_", " ")}`}</p>}
        {experiment.financial_context_status && <p className="text-sm text-muted-foreground">Case information: {experiment.financial_context_status.replaceAll("_", " ")}</p>}
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

  useEffect(() => {
    let active = true
    setLoading(true)
    getRuns()
      .then((items) => { if (active) { setRuns(items); setError(null) } })
      .catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : "Runs could not be loaded.") })
      .finally(() => { if (active) setLoading(false) })
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
                    <span className="mt-1 block font-mono text-xs text-muted-foreground">{run.id.slice(0, 8)}</span>
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
          <Route path="experiments" element={<LibraryPage />} />
          <Route path="experiments/:experimentId" element={<ExperimentPage />} />
          <Route path="coverage" element={<CoveragePage />} />
          <Route path="help" element={<HelpPage />} />
          <Route path="runs" element={<RunsPage />} />
          <Route path="runs/:runId" element={<RunPage />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
