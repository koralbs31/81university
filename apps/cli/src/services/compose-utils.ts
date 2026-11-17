// Both service images use the same release tag; each remains independently replaceable.
export function replaceComposeImageTag(compose: string, newImage: string): string {
  const apiImage = newImage.replace('-web:', '-api:')
  return compose
    .replace(/image:\s*ghcr\.io\/learnhouse\/learnhouse-web:\S+/, `image: ${newImage}`)
    .replace(/image:\s*ghcr\.io\/learnhouse\/learnhouse-api:\S+/, `image: ${apiImage}`)
}
