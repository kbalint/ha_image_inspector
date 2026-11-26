const strings = {
  en: {
    subtitle: 'Storage breakdown for the official Home Assistant Container image.',
    reference: 'Image tag or SHA-256 digest', architecture: 'Architecture', scan: 'Analyze image',
    resolving: 'Resolving the image manifest…', scanning: 'Reading layers',
    empty: 'Analyze an image to see which integrations, packages and system files take up space.',
    error: 'Analysis failed', compressed: 'Registry download', tar: 'Unpacked layer archives',
    visible: 'Visible files', hidden: 'Hidden file payload',
    compressedHelp: 'Compressed layer data', tarHelp: 'Includes tar headers and padding',
    visibleHelp: 'Regular files in the final image', hiddenHelp: 'Deleted or replaced in later layers',
    components: 'Components', layers: 'Layers', filter: 'Search components', all: 'All components',
    integration: 'Built-in integrations', python: 'Python packages', system: 'System libraries and tools',
    core: 'Home Assistant Core', other: 'Other paths', noMatches: 'No matching components.',
    files: 'files', biggest: 'Largest files', layer: 'Layer', payload: 'File payload',
    requirements: 'Declared Python dependencies present in the image', usedBy: 'Declared directly by',
    layerCaption: 'Base layer first. Select a layer to see its digest and build command.',
    attribution: 'Package ownership comes from Python installation records where available. Shared dependencies appear under their package names.',
    about: 'The official registry image is selected by tag or digest. Container changes, mounted volumes and Docker storage-driver overhead are outside this measurement.',
    more: 'Show more', menu: 'Open sidebar', of: 'of',
  },
  hu: {
    subtitle: 'A hivatalos Home Assistant Container image tárhelybontása.',
    reference: 'Image-tag vagy SHA-256 digest', architecture: 'Architektúra', scan: 'Image elemzése',
    resolving: 'Image-manifest beolvasása…', scanning: 'Rétegek beolvasása',
    empty: 'Indíts elemzést, hogy lásd, melyik integráció, csomag és rendszerfájl foglalja a helyet.',
    error: 'Az elemzés sikertelen', compressed: 'Registry-letöltés', tar: 'Kibontott rétegarchívumok',
    visible: 'Látható fájlok', hidden: 'Elfedezett fájlok',
    compressedHelp: 'Tömörített rétegek', tarHelp: 'A tar-fejlécekkel és kitöltéssel együtt',
    visibleHelp: 'A kész image normál fájljai', hiddenHelp: 'Későbbi réteg törölte vagy felülírta',
    components: 'Komponensek', layers: 'Rétegek', filter: 'Keresés a komponensek között', all: 'Összes komponens',
    integration: 'Beépített integrációk', python: 'Python-csomagok', system: 'Rendszerkönyvtárak és eszközök',
    core: 'Home Assistant Core', other: 'Egyéb útvonalak', noMatches: 'Nincs találat.',
    files: 'fájl', biggest: 'Legnagyobb fájlok', layer: 'Réteg', payload: 'Fájladatok',
    requirements: 'Az image-ben megtalálható közvetlen Python-függőségek', usedBy: 'Közvetlenül ezt használja',
    layerCaption: 'Az alapréteg van elöl. A digest és a buildparancs a réteg megnyitásával látható.',
    attribution: 'A Python-csomagokhoz a telepítési jegyzék rendeli a fájlokat, ahol elérhető. A megosztott függőségek a saját csomagnevük alatt látszanak.',
    about: 'A mérés a hivatalos registry taggel vagy digesttel kiválasztott image-ére vonatkozik. A konténer változásai, a csatolt kötetek és a Docker tárolódriverének többlete kívül esnek rajta.',
    more: 'További találatok', menu: 'Oldalsáv megnyitása', of: '/',
  },
};

const categories = ['integration', 'python', 'system', 'core', 'other'];
const escape = value => String(value).replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[char]);

class ImageInspectorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: 'open'});
    this.tab = 'components';
    this.category = '';
    this.query = '';
    this.limit = 40;
  }

  set hass(value) {
    this._hass = value;
    const locale = value.locale?.language || value.language || 'en';
    const language = locale.toLowerCase().startsWith('hu') ? 'hu' : 'en';
    if (this.language !== language) {
      this.language = language;
      this.t = strings[language];
      if (this.isConnected) this.render();
    }
    if (this.isConnected && !this.started) this.request();
  }

  connectedCallback() {
    if (this.t) this.render();
    if (this._hass && !this.started) this.request();
  }

  disconnectedCallback() {
    clearTimeout(this.timer);
    this.started = false;
  }

  async request(parameters = {}) {
    if (this.pending) return;
    this.started = true;
    this.pending = true;
    const form = this.shadowRoot.querySelector('form');
    if (form) form.querySelector('button').disabled = true;
    clearTimeout(this.timer);
    try {
      const state = await this._hass.callWS({type: 'ha_image_inspector/inspect', ...(this.report && {known: this.report.generated}), ...parameters});
      this.state = state;
      if (state.report || !form || !form.elements.reference.value) {
        this.report = state.report || this.report;
        if (this.isConnected) this.render();
      }
    } catch (error) {
      this.state = {...this.state, error: error.message || String(error), progress: null};
    } finally {
      this.pending = false;
      if (this.isConnected) {
        this.renderStatus();
        if (this.state?.progress) this.timer = setTimeout(() => this.request(), 2000);
      }
    }
  }

  format(bytes) {
    const unit = bytes >= 1e9 ? 'GB' : bytes >= 1e6 ? 'MB' : bytes >= 1e3 ? 'kB' : 'B';
    const divisor = {GB: 1e9, MB: 1e6, kB: 1e3, B: 1}[unit];
    return `${new Intl.NumberFormat(this.language, {maximumFractionDigits: 2}).format(bytes / divisor)} ${unit}`;
  }

  render() {
    const t = this.t;
    const previous = this.shadowRoot.querySelector('form');
    const reference = previous?.elements.reference.value || this.report?.reference || this.state?.default_reference || '';
    const architecture = (previous?.elements.reference.value && previous.elements.architecture.value) || this.report?.architecture || this.state?.default_architecture || 'amd64';
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="${new URL('./panel.css', import.meta.url)}">
      <header class="toolbar"><button class="menu" type="button" aria-label="${t.menu}"><ha-icon icon="mdi:menu"></ha-icon></button><span>HA Image Inspector</span></header>
      <main>
        <section class="hero">
          <div class="hero-copy"><div class="mark"><ha-icon icon="mdi:layers-triple-outline"></ha-icon></div>
          <h1>HA Image Inspector</h1><p>${t.subtitle}</p></div>
          <form>
            <label>${t.reference}<input name="reference" value="${escape(reference)}" required maxlength="135" spellcheck="false"></label>
            <label>${t.architecture}<select name="architecture"><option>amd64</option><option ${architecture === 'arm64' ? 'selected' : ''}>arm64</option></select></label>
            <button class="primary" type="submit"><ha-icon icon="mdi:magnify"></ha-icon>${t.scan}</button>
          </form>
          <div id="status" role="status" aria-live="polite"></div>
        </section>
        ${this.report ? this.summary() : `<div class="empty">${t.empty}</div>`}
        <footer>${t.about}</footer>
      </main>`;
    this.shadowRoot.querySelector('.menu').onclick = () => this.dispatchEvent(new Event('hass-toggle-menu', {bubbles: true, composed: true}));
    this.shadowRoot.querySelector('form').onsubmit = event => {
      event.preventDefault();
      const fields = event.currentTarget.elements;
      this.request({reference: fields.reference.value.trim(), architecture: fields.architecture.value});
    };
    this.renderStatus();
    if (this.report) this.renderTable();
  }

  renderStatus() {
    const box = this.shadowRoot.querySelector('#status');
    if (!box) return;
    const {progress, error} = this.state || {};
    this.shadowRoot.querySelector('form button').disabled = this.pending || !!progress;
    box.className = error ? 'error' : '';
    box.textContent = error ? `${this.t.error} · ${error}` : progress?.total
      ? `${this.t.scanning} ${progress.done} ${this.t.of} ${progress.total}` : progress ? this.t.resolving : '';
  }

  summary() {
    const {totals, groups, digest, reference, architecture, generated} = this.report;
    const t = this.t;
    const numbers = {compressed: totals.compressed, tar: totals.tar, visible: totals.visible, hidden: totals.written - totals.visible};
    const date = new Intl.DateTimeFormat(this.language, {dateStyle: 'medium', timeStyle: 'short'}).format(new Date(generated));
    const mix = categories.map(category => ({category, bytes: groups.filter(row => row.category === category).reduce((sum, row) => sum + row.bytes, 0)})).filter(row => row.bytes);
    const tag = reference.startsWith('sha256:') ? '' : `${escape(reference)} · `;
    return `<div class="stamp"><span>${tag}linux/${escape(architecture)} · ${date}</span><code title="${escape(digest)}">${escape(digest.slice(0, 23))}</code></div>
      <section class="metrics">${Object.entries(numbers).map(([key, value]) => `<article class="metric ${key}"><span>${t[key]}</span><strong>${this.format(value)}</strong><small>${t[`${key}Help`]}</small></article>`).join('')}</section>
      <div class="composition"><div class="stripe">${mix.map(row => `<span class="${row.category}" style="flex:${row.bytes}" title="${t[row.category]} · ${this.format(row.bytes)}"></span>`).join('')}</div>
      <div class="legend">${mix.map(row => `<span><i class="${row.category}"></i>${t[row.category]}</span>`).join('')}</div></div>
      <section class="table" id="table"></section>`;
  }

  renderTable() {
    const t = this.t;
    const table = this.shadowRoot.querySelector('#table');
    table.innerHTML = `<div class="table-header"><div class="tabs">${['components', 'layers'].map(tab => `<button type="button" data-tab="${tab}" aria-pressed="${this.tab === tab}">${t[tab]}</button>`).join('')}</div>
      ${this.tab === 'components' ? `<div class="filters"><input type="search" aria-label="${t.filter}" placeholder="${t.filter}" value="${escape(this.query)}"><select aria-label="${t.all}">${['', ...categories].map(category => `<option value="${category}" ${category === this.category ? 'selected' : ''}>${category ? t[category] : t.all}</option>`).join('')}</select></div>` : ''}</div>
      <p class="caption">${this.tab === 'components' ? t.attribution : t.layerCaption}</p><div id="rows"></div>`;
    table.querySelectorAll('[data-tab]').forEach(button => {
      button.onclick = () => { this.tab = button.dataset.tab; this.renderTable(); };
    });
    if (this.tab === 'components') {
      table.querySelector('input').oninput = event => { this.query = event.target.value; this.limit = 40; this.renderRows(); };
      table.querySelector('select').onchange = event => { this.category = event.target.value; this.limit = 40; this.renderRows(); };
    }
    this.renderRows();
  }

  renderRows() {
    const {groups, layers, totals} = this.report;
    const rows = this.shadowRoot.querySelector('#rows');
    const t = this.t;
    if (this.tab === 'layers') {
      rows.innerHTML = layers.map((layer, index) => `<details><summary><span><strong>${t.layer} ${index + 1}</strong><small>${escape(layer.digest.slice(0, 23))}</small></span><b>${this.format(layer.tar)}</b></summary><div class="details">
        <dl>${Object.entries({compressed: layer.compressed, payload: layer.written, visible: layer.visible, hidden: layer.written - layer.visible}).map(([key, value]) => `<div><dt>${t[key]}</dt><dd>${this.format(value)}</dd></div>`).join('')}</dl>
        <code>${escape(layer.digest)}</code>${layer.instruction ? `<pre>${escape(layer.instruction)}</pre>` : ''}</div></details>`).join('');
      return;
    }
    const query = this.query.trim().toLowerCase();
    const matches = groups.filter(group => (!this.category || this.category === group.category) && group.name.toLowerCase().includes(query));
    const largest = matches[0]?.bytes || 1;
    rows.innerHTML = matches.slice(0, this.limit).map(group => `<details><summary><span><strong>${escape(group.name)}</strong><small>${t[group.category]} · ${group.files.toLocaleString(this.language)} ${t.files}</small></span>
      <span class="bar" aria-hidden="true"><i class="${group.category}" style="width:${group.bytes / largest * 100}%"></i></span><b>${this.format(group.bytes)}<small>${(group.bytes / (totals.visible || 1)).toLocaleString(this.language, {style: 'percent', maximumFractionDigits: 2})}</small></b></summary>
      <div class="details">${group.related?.length ? `<p><strong>${group.category === 'integration' ? t.requirements : t.usedBy}</strong><br>${group.related.map(escape).join(', ')}</p>` : ''}
      <strong>${t.biggest}</strong>${group.largest.map(file => `<div class="file"><code>${escape(file.path)}</code><b>${this.format(file.bytes)}</b></div>`).join('')}</div></details>`).join('') || `<p class="caption">${t.noMatches}</p>`;
    if (matches.length > this.limit) {
      const button = document.createElement('button');
      button.className = 'more';
      button.textContent = t.more;
      button.onclick = () => { this.limit += 40; this.renderRows(); };
      rows.append(button);
    }
  }
}

customElements.define('ha-image-inspector', ImageInspectorPanel);
