import Image from 'next/image'
import { branding } from '@/lib/branding'

export default function BrandLogo() {
  return (
    <Image
      src={branding.logo}
      alt={branding.name}
      width={138}
      height={45}
      className="h-10 w-auto shrink-0 object-contain"
    />
  )
}
