import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { defineConfig, type Plugin } from 'vite'

// Derlenmiş arayüz (dist/) depoda durur; sunucuda Node gerekmez. Kaynağı değiştirip derlemeyi unutmamak için
// derlemeye kaynak dosyaların özeti yazılır; Python testi (tests/test_arayuz.py) aynı özeti hesaplayıp karşılaştırır.
// Bu listeyi değiştirirseniz testteki listeyi de değiştirin.
const KAYNAKLAR = ['src', 'public', 'index.html', 'package.json', 'package-lock.json', 'vite.config.ts',
  'tsconfig.json', 'tsconfig.app.json', 'tsconfig.node.json']

function dosyalar(yol: string): string[] {
  return statSync(yol).isDirectory() ? readdirSync(yol).flatMap((ad) => dosyalar(join(yol, ad))) : [yol]
}

export function kaynakOzeti(kok: string): string {
  const ozet = createHash('sha256')
  const liste = KAYNAKLAR.flatMap((k) => dosyalar(join(kok, k)))
    .map((d) => relative(kok, d).split(sep).join('/'))
    .sort()
  for (const ad of liste) {
    // Satır sonları (Windows CRLF / Linux LF) özeti değiştirmesin.
    const icerik = readFileSync(join(kok, ad)).filter((b) => b !== 13)
    ozet.update(ad + '\0').update(icerik).update('\0')
  }
  return ozet.digest('hex')
}

const kaynakOzetiYaz = (): Plugin => ({
  name: 'kaynak-ozeti',
  apply: 'build',
  generateBundle() {
    this.emitFile({ type: 'asset', fileName: 'kaynak-ozeti.txt', source: kaynakOzeti(__dirname) + '\n' })
  },
})

export default defineConfig({
  plugins: [react(), tailwindcss(), kaynakOzetiYaz()],
  server: {
    // Geliştirme: `npm run dev` (5173) + `mevzuat.cli panel` (8005). API istekleri panele aktarılır;
    // tarayıcı açısından tek adres olduğu için çerez ve CSRF üretimdeki gibi çalışır.
    proxy: { '/api': 'http://127.0.0.1:8005', '/panel/api': 'http://127.0.0.1:8005' },
  },
})
