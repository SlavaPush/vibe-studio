import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'

const contractUrl = new URL('../../contracts/api.md', import.meta.url)
const outputUrl = new URL('../src/mocks/contract-examples.ts', import.meta.url)

const text = readFileSync(contractUrl, 'utf8')
const markers = text.match(/<!-- (example|enum): [A-Za-z]\w* -->/g) ?? []
const blocks = [...text.matchAll(/<!-- (example|enum): ([A-Za-z]\w*) -->\n```json\n([\s\S]*?)\n```/g)]

if (markers.length !== blocks.length) {
  throw new Error(`contracts/api.md: ${markers.length} markers, ${blocks.length} JSON blocks`)
}

const examples = new Map()
const enums = new Map()
for (const [, kind, name, body] of blocks) {
  const value = JSON.parse(body)
  if (kind === 'example') {
    examples.set(name, [...(examples.get(name) ?? []), value])
  } else {
    enums.set(name, value)
  }
}

const typeNames = [...new Set([...examples.keys(), ...enums.keys()])].sort()
const indent = (json) => json.replace(/\n/g, '\n    ')

const exampleLines = [...examples].map(
  ([name, values]) =>
    `  ${name}: [\n${values
      .map((value) => `    ${indent(JSON.stringify(value, null, 2))} satisfies ${name},`)
      .join('\n')}\n  ],`,
)
const enumLines = [...enums].map(
  ([name, values]) =>
    `  ${name}: {\n${values
      .map((value) => `    ${JSON.stringify(value)}: true,`)
      .join('\n')}\n  } satisfies Record<${name}, true>,`,
)

const output = `import type {
${typeNames.map((name) => `  ${name},`).join('\n')}
} from '@/api/types'

export const contractExamples = {
${exampleLines.join('\n')}
}

export const contractEnums = {
${enumLines.join('\n')}
}
`

mkdirSync(new URL('.', outputUrl), { recursive: true })
writeFileSync(outputUrl, output)
