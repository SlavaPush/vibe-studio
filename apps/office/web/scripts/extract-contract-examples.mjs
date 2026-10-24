import { readFileSync, writeFileSync } from 'node:fs'

const contract = readFileSync(new URL('../../contracts/api.md', import.meta.url), 'utf8')
const pattern = /<!-- example: (\S+) -->\s*```json\n([\s\S]*?)\n```/g
const examples = {}

for (const [, name, body] of contract.matchAll(pattern)) {
  examples[name] ??= []
  examples[name].push(JSON.parse(body))
}

if (Object.keys(examples).length === 0) {
  throw new Error('contracts/api.md: примеры не найдены')
}

writeFileSync(
  new URL('../src/mocks/contract-examples.json', import.meta.url),
  `${JSON.stringify(examples, null, 2)}\n`,
)
