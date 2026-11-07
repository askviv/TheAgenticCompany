import { useEffect, useRef, useState } from "react"
import { getCompanyPack, getCompanyPacks, importCompanyPack, uploadCompanyPack, type CompanyPackImport as Pack } from "@/lib/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

function failureMessage(reason: unknown) {
  if (reason instanceof Error) {
    if (reason.message.includes(": pack_invalid_archive")) return "The ZIP does not match the company pack schema. Check the manifest, referenced files and archive limits, then upload the corrected ZIP."
    if (reason.message.includes(": pack_version_conflict")) return "This pack ID and version already contain a different ZIP. Use a new version for changed contents."
    if (reason.message.includes(": pack_clock_unavailable")) return "The company clock is unavailable. The operator must restore it; then retry this saved import."
    return reason.message
  }
  return "The company pack request failed. Please try again."
}

function PackPreview({ pack, onUpdate, onImported }: { pack: Pack; onUpdate: (pack: Pack) => void; onImported: () => void }) {
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [connection, setConnection] = useState(0)
  const completed = useRef(pack.status === "completed")
  const active = pack.status === "queued" || pack.status === "importing"
  const updateRef = useRef(onUpdate)
  updateRef.current = onUpdate

  useEffect(() => {
    if (pack.status === "completed" && !completed.current) onImported()
    completed.current = pack.status === "completed"
  }, [pack.status, onImported])

  useEffect(() => {
    if (!active) return
    const source = new EventSource(`/api/company-packs/${encodeURIComponent(pack.id)}/events`)
    source.onmessage = (event) => {
      try {
        const update = JSON.parse(event.data) as Pack
        updateRef.current(update)
        setError(null)
        if (update.status !== "queued" && update.status !== "importing") source.close()
      } catch (reason) {
        source.close()
        setError(`Import status could not be read: ${failureMessage(reason)}`)
      }
    }
    source.onerror = () => {
      source.close()
      setError("Live status disconnected. Refresh status to reconnect; the server keeps the saved import progress.")
    }
    return () => source.close()
  }, [pack.id, active, connection])

  async function act(importPack: boolean) {
    setBusy(true)
    setError(null)
    try {
      onUpdate(await (importPack ? importCompanyPack(pack.id) : getCompanyPack(pack.id)))
      if (!importPack) setConnection((value) => value + 1)
    } catch (reason) {
      setError(failureMessage(reason))
    } finally {
      setBusy(false)
    }
  }

  return <article className="space-y-4 rounded-lg border p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="font-medium">{pack.title} <span className="text-sm text-muted-foreground">v{pack.version}</span></h3>
      <Badge variant="secondary">{pack.status}</Badge>
    </div>
    <div className="space-y-1 text-sm">
      <p className="font-medium">Company persona: {pack.persona.name}</p>
      <p><span className="text-muted-foreground">Business type:</span> {pack.persona.business_type}</p>
      <p className="whitespace-pre-wrap">{pack.persona.description}</p>
      <p><span className="text-muted-foreground">Enabled apps:</span> {pack.persona.applications.join(", ")}</p>
    </div>
    <dl className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
      {Object.entries(pack.counts).map(([label, count]) => <div key={label}><dt className="inline capitalize text-muted-foreground">{label}: </dt><dd className="inline font-medium">{count}</dd></div>)}
    </dl>
    {pack.status !== "validated" && <div className="space-y-1 text-sm" aria-live="polite">
      <p>{pack.completed_steps} of {pack.total_steps} import steps completed</p>
      <progress className="h-2 w-full" aria-label={`${pack.title} import progress`} value={pack.completed_steps} max={pack.total_steps || 1} />
      {pack.status === "completed" && <p>Company imported. Its experiments are available in the library. Choose an experiment to run it.</p>}
    </div>}
    {(pack.error || error) && <Alert variant="destructive"><AlertTitle>Import needs attention</AlertTitle><AlertDescription>{error || pack.error}</AlertDescription></Alert>}
    <div className="flex flex-wrap gap-2">
      {(pack.status === "validated" || pack.status === "failed") && <Button disabled={busy} onClick={() => void act(true)}>{busy ? "Submitting…" : pack.status === "failed" ? "Retry import" : "Import company"}</Button>}
      <Button variant="outline" disabled={busy} onClick={() => void act(false)}>Refresh status</Button>
    </div>
  </article>
}

export function CompanyPackImport({ onImported }: { onImported: () => void }) {
  const [packs, setPacks] = useState<Pack[]>([])
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reload, setReload] = useState(0)
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let active = true
    setLoading(true)
    getCompanyPacks().then((items) => {
      if (active) { setPacks(items); setError(null) }
    }).catch((reason: unknown) => {
      if (active) setError(failureMessage(reason))
    }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [reload])

  function update(pack: Pack) {
    setPacks((items) => [pack, ...items.filter((item) => item.id !== pack.id)])
  }

  async function upload(file: File) {
    setBusy(true)
    setError(null)
    try { update(await uploadCompanyPack(file)) }
    catch (reason) { setError(failureMessage(reason)) }
    finally {
      setBusy(false)
      if (input.current) input.current.value = ""
    }
  }

  return <Card>
    <CardHeader>
      <CardTitle>Import a company pack</CardTitle>
      <CardDescription>Upload a ZIP to validate and preview the full company persona, staff, data and experiments. Importing seeds the company; it does not run experiments.</CardDescription>
    </CardHeader>
    <CardContent className="space-y-4">
      <div className="rounded-lg border border-dashed p-6 text-center" onDragOver={(event) => event.preventDefault()} onDrop={(event) => {
        event.preventDefault()
        const file = event.dataTransfer.files[0]
        if (file && !busy) void upload(file)
      }}>
        <p className="mb-3 text-sm text-muted-foreground">Drop a company ZIP here, or choose a file.</p>
        <input ref={input} type="file" accept=".zip,application/zip" className="sr-only" aria-label="Company pack ZIP" disabled={busy} onChange={(event) => {
          const file = event.target.files?.[0]
          if (file) void upload(file)
        }} />
        <Button variant="outline" disabled={busy} onClick={() => input.current?.click()}>{busy ? "Uploading and validating…" : "Choose ZIP to validate"}</Button>
      </div>
      {error && <Alert variant="destructive"><AlertTitle>Company packs unavailable</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}
      <Button variant="ghost" disabled={loading} onClick={() => setReload((value) => value + 1)}>{loading ? "Loading saved imports…" : "Reload saved imports"}</Button>
      {packs.map((pack) => <PackPreview key={pack.id} pack={pack} onUpdate={update} onImported={onImported} />)}
    </CardContent>
  </Card>
}
