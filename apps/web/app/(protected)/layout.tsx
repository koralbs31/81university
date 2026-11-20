import DocumentLayout from '../document-layout'
export { metadata, viewport } from '../document-layout'

export default function Layout({ children }: { children: React.ReactNode }) {
  return <DocumentLayout>{children}</DocumentLayout>
}
