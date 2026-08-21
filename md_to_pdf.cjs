#!/usr/bin/env node
/**
 * md_to_pdf.cjs — markdown → PDF via the Chromium already on this machine.
 *
 * No pandoc, no weasyprint, no LaTeX. The build installs Node Playwright and
 * caches Chromium for the smoke test, and a browser that can lay out a page can
 * print one. Adding a document toolchain for one PDF would be a second
 * dependency tree to keep alive.
 *
 * The markdown subset is deliberately small — headings, tables, bold, italic,
 * code, lists, rules — because that is all build_audit_pack.py emits. A general
 * markdown parser here would be code with no caller.
 *
 * Usage: node md_to_pdf.cjs input.md output.pdf ["Title"]
 */
const fs = require('fs');
const { chromium } = require('playwright');

const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
}

function render(md) {
  const out = [];
  const lines = md.split('\n');
  let i = 0, inList = false;

  const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };

  while (i < lines.length) {
    const l = lines[i];

    // table: header row, separator, body
    if (/^\|/.test(l) && /^\|[\s:|-]+\|$/.test(lines[i + 1] || '')) {
      closeList();
      const cells = r => r.split('|').slice(1, -1).map(c => c.trim());
      out.push('<table><thead><tr>' +
        cells(l).map(c => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>');
      i += 2;
      while (i < lines.length && /^\|/.test(lines[i])) {
        out.push('<tr>' + cells(lines[i]).map(c => `<td>${inline(c)}</td>`).join('') + '</tr>');
        i++;
      }
      out.push('</tbody></table>');
      continue;
    }
    if (/^### /.test(l))      { closeList(); out.push(`<h3>${inline(l.slice(4))}</h3>`); }
    else if (/^## /.test(l))  { closeList(); out.push(`<h2>${inline(l.slice(3))}</h2>`); }
    else if (/^# /.test(l))   { closeList(); out.push(`<h1>${inline(l.slice(2))}</h1>`); }
    else if (/^---\s*$/.test(l)) { closeList(); out.push('<hr>'); }
    else if (/^[-*] /.test(l)) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push(`<li>${inline(l.replace(/^[-*] /, ''))}</li>`);
    }
    else if (l.trim() === '') { closeList(); }
    else { closeList(); out.push(`<p>${inline(l)}</p>`); }
    i++;
  }
  closeList();
  return out.join('\n');
}

const CSS = `
  @page { margin: 18mm 16mm; }
  body { font: 10.5pt/1.55 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
         color: #16181d; }
  h1 { font-size: 21pt; margin: 0 0 4pt; letter-spacing: -.4pt; }
  h2 { font-size: 14pt; margin: 20pt 0 6pt; padding-top: 8pt;
       border-top: 1px solid #dcdfe4; }
  h3 { font-size: 11.5pt; margin: 15pt 0 4pt; color: #000;
       page-break-after: avoid; }
  p { margin: 0 0 7pt; }
  ul { margin: 0 0 8pt; padding-left: 16pt; }
  li { margin-bottom: 3pt; }
  hr { border: 0; border-top: 1px solid #dcdfe4; margin: 14pt 0; }
  code { font: 9.5pt/1.4 "SF Mono", Menlo, Consolas, monospace;
         background: #f1f3f5; padding: 1pt 3pt; border-radius: 2pt; }
  table { border-collapse: collapse; width: 100%; margin: 8pt 0 12pt;
          font-size: 9pt; page-break-inside: avoid; }
  th { text-align: left; border-bottom: 1.2pt solid #16181d; padding: 4pt 6pt;
       font-size: 8pt; text-transform: uppercase; letter-spacing: .4pt; }
  td { border-bottom: .5pt solid #e6e8ec; padding: 4pt 6pt;
       font-variant-numeric: tabular-nums; }
  /* Numeric columns right-align so the figures line up on the decimal. */
  td:nth-child(n+2) { text-align: right; }
  td:last-child, th:last-child { text-align: left; }
  strong { font-weight: 650; }
`;

(async () => {
  const [, , input, output, title] = process.argv;
  if (!input || !output) { console.error('usage: md_to_pdf.cjs in.md out.pdf [title]'); process.exit(2); }
  const html = `<!doctype html><meta charset="utf-8"><title>${esc(title || 'Document')}</title>` +
               `<style>${CSS}</style>${render(fs.readFileSync(input, 'utf8'))}`;
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    await page.setContent(html, { waitUntil: 'load' });
    await page.pdf({
      path: output, format: 'A4', printBackground: true,
      displayHeaderFooter: true,
      headerTemplate: '<div></div>',
      footerTemplate: '<div style="font:8pt sans-serif;color:#8a9099;width:100%;' +
                      'padding:0 16mm;display:flex;justify-content:space-between">' +
                      '<span>The Daily Signal — engine audit pack</span>' +
                      '<span class="pageNumber"></span></div>',
      margin: { top: '18mm', bottom: '16mm', left: '16mm', right: '16mm' },
    });
    console.error('wrote ' + output);
  } finally { await browser.close(); }
})();
