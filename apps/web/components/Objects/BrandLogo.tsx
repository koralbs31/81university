import Image from 'next/image'
import { branding } from '@/lib/branding'

export default function BrandLogo() {
  return (
    <span className="inline-flex shrink-0 items-center gap-1 rounded bg-white px-1 py-0.5" aria-label={branding.name}>
      <Image src={branding.icon} alt="" width={36} height={32} className="h-8 w-9 object-contain" />
      <span className="text-xl font-bold tracking-tight text-gray-950" dir="ltr">University</span>
    </span>
  )
}
