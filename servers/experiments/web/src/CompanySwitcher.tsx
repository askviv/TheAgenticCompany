import { useEffect, useState } from "react"
import { changeCompanyEnvironment, getCompanyEnvironmentJob, getCompanyEnvironments, renameCompanyEnvironment, startCompanyEnvironmentJob, type CompanyEnvironmentJob, type CompanyEnvironments } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert"

const storageKey = "company-environment-operation"
function message(reason: unknown) { return reason instanceof Error ? reason.message : "The company environment request failed." }

export function CompanySwitcher() {
  const [environments, setEnvironments] = useState<CompanyEnvironments | null>(null)
  const [selected, setSelected] = useState("")
  const [name, setName] = useState("")
  const [job, setJob] = useState<CompanyEnvironmentJob | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [storageError, setStorageError] = useState<string | null>(null)
  const [observation, setObservation] = useState(0)
  const active = job?.status === "queued" || job?.status === "running"
  const listedOperation = environments?.operation
  const listedActive = (listedOperation?.status === "queued" || listedOperation?.status === "running")
    && !(job?.id === listedOperation.id && (job.status === "completed" || job.status === "failed"))
  const locked = busy || active || listedActive

  function rememberJob(value: CompanyEnvironmentJob | null) {
    try {
      if (value) sessionStorage.setItem(storageKey, JSON.stringify(value))
      else sessionStorage.removeItem(storageKey)
      setStorageError(null)
    } catch (reason) {
      setStorageError(`This browser could not save operation status for a page reload. Keep this page open. ${message(reason)}`)
    }
  }

  useEffect(() => {
    let mounted = true
    const controller = new AbortController()
    try {
      const saved = sessionStorage.getItem(storageKey)
      if (saved) setJob(JSON.parse(saved) as CompanyEnvironmentJob)
    } catch (reason) { setStorageError(`Saved company operation could not be read. ${message(reason)}`) }
    getCompanyEnvironments(AbortSignal.any([controller.signal, AbortSignal.timeout(30000)])).then((value) => {
      if (!mounted) return
      setEnvironments(value)
      setSelected(value.active_id)
      const operation = value.operation
      if (operation) setJob((saved) => saved && saved.id === operation.id
        ? { ...operation, status_token: operation.status_token ?? saved.status_token }
        : operation)
    }).catch((reason: unknown) => { if (mounted) setError(message(reason)) })
    return () => { mounted = false; controller.abort() }
  }, [])

  useEffect(() => {
    if (!job || (job.status !== "queued" && job.status !== "running")) return
    if (!job.status_token) {
      setError("A company environment operation is active. This browser has no status token for it; reload the company list once the operation finishes.")
      return
    }
    let mounted = true
    const controller = new AbortController()
    let timer: number | undefined
    const deadline = Date.now() + 15 * 60 * 1000
    const id = job.id
    const token = job.status_token
    async function observe() {
      try {
        const update = await getCompanyEnvironmentJob(id, token, AbortSignal.any([controller.signal, AbortSignal.timeout(30000)]))
        if (!mounted) return
        const saved = { ...update, status_token: token }
        setJob(saved)
        setError(null)
        rememberJob(saved)
        if (update.status === "completed") {
          rememberJob(null)
          window.location.reload()
          return
        }
        if (update.status === "failed") {
          try {
            const value = await getCompanyEnvironments(AbortSignal.any([controller.signal, AbortSignal.timeout(30000)]))
            if (mounted) {
              setEnvironments(value)
              setSelected(value.active_id)
            }
          } catch (reason) {
            if (mounted) setError(`The company list could not be refreshed after recovery. Reload the company list. ${message(reason)}`)
          }
          return
        }
        if (Date.now() >= deadline) {
          setError("Status observation reached its time limit. Resume status to check the durable operation.")
          return
        }
        // Observe durable host state without overlapping requests; bounded to 15 minutes.
        timer = window.setTimeout(() => void observe(), 1500)
      } catch (reason) {
        if (mounted) setError(`Company operation status unavailable. ${message(reason)} Resume status to retry.`)
      }
    }
    async function begin() {
      try {
        if (job?.status === "queued" || (job?.status === "running" && job.error)) {
          await startCompanyEnvironmentJob(id, token, AbortSignal.any([controller.signal, AbortSignal.timeout(30000)]))
        }
        if (mounted) await observe()
      } catch (reason) {
        if (mounted) setError(`Company operation acceptance could not be confirmed. ${message(reason)} Resume status to retry the same operation.`)
      }
    }
    void begin()
    return () => { mounted = false; controller.abort(); window.clearTimeout(timer) }
  }, [job?.id, job?.status_token, observation])

  async function change(action: "clone" | "switch") {
    setBusy(true)
    setError(null)
    try {
      const started = await changeCompanyEnvironment(action, selected, action === "clone" ? name : undefined)
      // Retain the host capability across service transitions and page reload.
      setJob(started)
      rememberJob(started)
    } catch (reason) { setError(message(reason)) }
    finally { setBusy(false) }
  }

  async function rename() {
    if (!environments) return
    setBusy(true)
    setError(null)
    try { setEnvironments(await renameCompanyEnvironment(environments.active_id, name)) }
    catch (reason) { setError(message(reason)) }
    finally { setBusy(false) }
  }

  return <details className="mx-4 mt-4 rounded-lg border bg-card p-4 sm:mx-6">
    <summary className="cursor-pointer text-sm font-medium">Company environment: {environments?.companies.find((company) => company.active)?.name ?? "Loading…"}{active ? ` · ${job?.stage}` : ""}</summary>
    <div className="mt-4 space-y-4">
      <p className="text-sm text-muted-foreground">Switching or duplicating briefly pauses company services. Inactive companies keep their work and their clocks stay paused.</p>
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="company-environment" className="text-sm">Company</label>
        <select id="company-environment" className="rounded-md border bg-background p-2 text-sm" value={selected} disabled={locked || !environments} onChange={(event) => setSelected(event.target.value)}>
          {environments?.companies.map((company) => <option key={company.id} value={company.id}>{company.name}{company.active ? " (active)" : ""}</option>)}
        </select>
        <Button variant="outline" disabled={locked || !selected || selected === environments?.active_id} onClick={() => void change("switch")}>Switch company</Button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Input aria-label="Company name" placeholder="Company name" value={name} disabled={locked} onChange={(event) => setName(event.target.value)} className="max-w-xs" />
        <Button disabled={locked || !environments || !name.trim()} onClick={() => void change("clone")}>Duplicate active company</Button>
        <Button variant="outline" disabled={locked || !environments || !name.trim()} onClick={() => void rename()}>Rename active company</Button>
      </div>
      {job && <div className="space-y-2 text-sm" aria-live="polite">
        <p>{job.operation}: {job.status} — {job.stage}</p>
        {job.error && <p className="text-destructive">{job.error}</p>}
        {active && <Button variant="outline" onClick={() => setObservation((value) => value + 1)}>{job.error ? "Resume operation" : "Resume status"}</Button>}
      </div>}
      {error && <Alert variant="destructive"><AlertTitle>Company environment needs attention</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}
      {storageError && <Alert><AlertTitle>Operation status cannot be saved in this browser</AlertTitle><AlertDescription>{storageError}</AlertDescription></Alert>}
      <Button variant="ghost" disabled={busy} onClick={() => window.location.reload()}>Reload company list</Button>
    </div>
  </details>
}
