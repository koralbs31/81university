import type { Metadata } from 'next'

export const metadata: Metadata = { title: 'Software notices · 81 University' }

export default function SoftwareNotices() {
  return (
    <article dir="ltr" lang="en" className="mx-auto max-w-3xl space-y-6 px-6 py-16 text-neutral-800 [&_a]:underline [&_a]:underline-offset-4">
      <a href="/">← 81 University</a>
      <h1 className="text-3xl font-semibold">Software notices</h1>
      <p>81 University is a modified version of <a href="https://github.com/learnhouse/learnhouse">LearnHouse</a>. Original software copyright remains with the LearnHouse authors and contributors. Copyright in modifications remains with their respective authors.</p>
      <p>Modified by the 81 University project. Modification notice dated 19 November 2026. Changes include Hebrew and Arabic support, 81 University branding, simplified features, and deployment changes. The repository records subsequent changes and their dates.</p>
      <h2 className="text-xl font-semibold">License and source code</h2>
      <p>This modified application is licensed under the GNU Affero General Public License, version 3. You may copy, modify and redistribute the covered software under that license.</p>
      <p><a href="https://github.com/koralbs31/81university">Get the corresponding source code free of charge</a>, including modifications and build and installation instructions. The repository history retains the source for released revisions; deployment image tags identify their source commit.</p>
      <p><a href="/AGPL-3.0.txt">Read or download the full AGPL-3.0 license</a>.</p>
      <p>The software is provided without warranty, including implied warranties of merchantability or fitness for a particular purpose, to the extent permitted by law. See the license for details.</p>
      <h2 className="text-xl font-semibold">Other notices</h2>
      <p>Third-party components retain their own licenses and notices in the source repository and dependency packages. LearnHouse is the upstream project’s name; 81 University is an independently modified deployment.</p>
    </article>
  )
}
