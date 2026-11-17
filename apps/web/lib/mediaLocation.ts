export function positiveIntParam(search: string, name: string): number | null {
  const raw = new URLSearchParams(search).get(name)
  if (!raw || !/^\d+$/.test(raw)) return null
  const value = Number(raw)
  return value > 0 ? value : null
}
