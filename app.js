/**
 * IDEA DUMP: Autonomous Engineering Pipeline Client
 * Connects to Supabase PostgreSQL or falls back to local storage demo mode.
 */

// State Management
const STATE = {
  supabase: null,
  isDemoMode: true,
  ideas: [],
  activeFilter: 'all',
  searchQuery: '',
  stagedUrls: [],
  stagedTags: new Set(),
  storageKey: 'ideadump_local_cache_v1',
  configKey: 'ideadump_supabase_config_v1'
};

// Seed sample ideas for instant demo experience
const DEMO_SEED_IDEAS = [
  {
    id: 1,
    title: 'Autonomous Market Scraping & Fast Parity Check',
    raw_content: 'Ingest marktplaats & ebay listings via Scrapling crawler and use DuckDB local parity check before triggering apify. Reduce latency by 90% and save API credits.',
    urls: ['https://github.com/dreadnode/scrapling', 'https://duckdb.org'],
    tags: ['scraping', 'automation', 'database'],
    status: 'pending',
    priority: 'high',
    created_at: new Date(Date.now() - 3600000 * 4).toISOString(),
    day_of_week: 'Sunday',
    github_repo_url: null
  },
  {
    id: 2,
    title: 'Telegram Mini-App for Instant Voice Idea Dictation',
    raw_content: 'Record quick voice notes on phone -> Groq Whisper transcription -> push directly to Supabase ideas table with auto-tagged topics.',
    urls: ['https://groq.com', 'https://core.telegram.org/bots/webapps'],
    tags: ['ai-agent', 'telegram', 'whisper'],
    status: 'built',
    priority: 'normal',
    created_at: new Date(Date.now() - 86400000 * 2).toISOString(),
    day_of_week: 'Friday',
    github_repo_url: 'https://github.com/knarayanareddy/idea-voice-dictation-bot'
  },
  {
    id: 3,
    title: 'OpenSpec Automated Change Proposal Generator',
    raw_content: 'Feed high-level feature requirements into Cline Gemini 3.8 Flash to spit out full OpenSpec change markdown folders with acceptance criteria and tasks.',
    urls: ['https://github.com/dreadnode/openspec'],
    tags: ['sdd', 'gemini', 'tooling'],
    status: 'researching',
    priority: 'urgent',
    created_at: new Date(Date.now() - 86400000).toISOString(),
    day_of_week: 'Saturday',
    github_repo_url: null
  }
];

// DOM Elements
const DOM = {
  // Config & Status
  connectionStatus: document.getElementById('connection-status'),
  connectionLabel: document.getElementById('connection-label'),
  openConfigBtn: document.getElementById('open-config-btn'),
  configModal: document.getElementById('config-modal'),
  closeModalBtn: document.getElementById('close-modal-btn'),
  saveConfigBtn: document.getElementById('save-config-btn'),
  clearConfigBtn: document.getElementById('clear-config-btn'),
  supabaseUrlInput: document.getElementById('supabase-url-input'),
  supabaseKeyInput: document.getElementById('supabase-key-input'),

  // Stats
  statTotal: document.getElementById('stat-total'),
  statPending: document.getElementById('stat-pending'),
  statBuilt: document.getElementById('stat-built'),
  filteredCount: document.getElementById('filtered-count'),

  // Form
  ideaForm: document.getElementById('idea-form'),
  ideaTitle: document.getElementById('idea-title'),
  ideaContent: document.getElementById('idea-content'),
  ideaPriority: document.getElementById('idea-priority'),
  linkInput: document.getElementById('link-input'),
  addLinkBtn: document.getElementById('add-link-btn'),
  urlChips: document.getElementById('url-chips'),
  tagChips: document.querySelectorAll('.tag-chip'),
  customTagInput: document.getElementById('custom-tag-input'),
  activeTags: document.getElementById('active-tags'),
  submitBtn: document.getElementById('submit-idea-btn'),
  formMessage: document.getElementById('form-message'),

  // Feed & Filters
  searchInput: document.getElementById('search-input'),
  filterTabs: document.querySelectorAll('.filter-tab'),
  ideasFeed: document.getElementById('ideas-feed'),
  toastContainer: document.getElementById('toast-container')
};

// -----------------------------------------------------------------------------
// Initialization & Supabase Setup
// -----------------------------------------------------------------------------
async function initApp() {
  setupEventListeners();
  loadSavedConfig();
  await loadIdeas();
  renderApp();
}

// Default Supabase project credentials for idea-dump
const SUPABASE_DEFAULT_URL = 'https://akvklicuxnpgunivmrvo.supabase.co';
const SUPABASE_DEFAULT_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFrdmtsaWN1eG5wZ3VuaXZtcnZvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTA1MzMzNDQsImV4cCI6MjEwNjEwOTM0NH0.tNq04eaZjzkFUmiABQhZ9Rwpcky1vgUdukRjvV6DZvo';

function loadSavedConfig() {
  try {
    let url = SUPABASE_DEFAULT_URL;
    let key = SUPABASE_DEFAULT_ANON_KEY;

    const raw = localStorage.getItem(STATE.configKey);
    if (raw) {
      const config = JSON.parse(raw);
      if (config.mode === 'demo') {
        // Explicitly requested demo mode
        STATE.isDemoMode = true;
        updateConnectionBadge(false);
        return;
      }
      if (config.url && config.key) {
        url = config.url;
        key = config.key;
      }
    }

    if (url && key && window.supabase) {
      STATE.supabase = window.supabase.createClient(url, key);
      STATE.isDemoMode = false;
      updateConnectionBadge(true);
      DOM.supabaseUrlInput.value = url;
      DOM.supabaseKeyInput.value = key;
      return;
    }
  } catch (err) {
    console.error('Config load error:', err);
  }

  // Fallback to Demo Mode if client cannot be created
  STATE.isDemoMode = true;
  updateConnectionBadge(false);
}

function updateConnectionBadge(isConnected) {
  if (isConnected) {
    DOM.connectionStatus.className = 'status-indicator status-connected';
    DOM.connectionLabel.textContent = 'Connected (Supabase)';
  } else {
    DOM.connectionStatus.className = 'status-indicator status-demo';
    DOM.connectionLabel.textContent = 'Demo Mode (Local)';
  }
}

// -----------------------------------------------------------------------------
// Data Fetching & Sync
// -----------------------------------------------------------------------------
async function loadIdeas() {
  if (!STATE.isDemoMode && STATE.supabase) {
    try {
      const { data, error } = await STATE.supabase
        .from('ideas')
        .select('*')
        .order('created_at', { ascending: false });

      if (error) throw error;
      STATE.ideas = data || [];
      return;
    } catch (err) {
      console.warn('Supabase fetch failed, falling back to local storage:', err);
      showToast('Supabase sync error: using local cache', 'error');
    }
  }

  // Local Storage fallback
  const cached = localStorage.getItem(STATE.storageKey);
  if (cached) {
    try {
      STATE.ideas = JSON.parse(cached);
    } catch (e) {
      STATE.ideas = DEMO_SEED_IDEAS;
    }
  } else {
    STATE.ideas = DEMO_SEED_IDEAS;
    saveLocalIdeas();
  }
}

function saveLocalIdeas() {
  localStorage.setItem(STATE.storageKey, JSON.stringify(STATE.ideas));
}

// -----------------------------------------------------------------------------
// Submitting New Ideas
// -----------------------------------------------------------------------------
async function handleIdeaSubmit(e) {
  if (e) e.preventDefault();

  const content = DOM.ideaContent.value.trim();
  if (!content) {
    DOM.ideaContent.focus();
    return;
  }

  // Derive title if empty
  let title = DOM.ideaTitle.value.trim();
  if (!title) {
    title = content.split('\n')[0].substring(0, 70);
    if (content.length > 70) title += '...';
  }

  // Auto-extract any URLs present in content body
  const extractedUrls = extractUrlsFromText(content);
  const combinedUrls = Array.from(new Set([...STATE.stagedUrls, ...extractedUrls]));
  const tags = Array.from(STATE.stagedTags);
  const priority = DOM.ideaPriority.value;

  const now = new Date();
  const dayName = now.toLocaleDateString('en-US', { weekday: 'long' });

  const newIdea = {
    title,
    raw_content: content,
    urls: combinedUrls,
    tags: tags.length ? tags : ['unclassified'],
    status: 'pending',
    priority,
    created_at: now.toISOString(),
    day_of_week: dayName,
    github_repo_url: null
  };

  DOM.submitBtn.disabled = true;

  try {
    if (!STATE.isDemoMode && STATE.supabase) {
      const { data, error } = await STATE.supabase
        .from('ideas')
        .insert([newIdea])
        .select();

      if (error) throw error;
      if (data && data.length) {
        STATE.ideas.unshift(data[0]);
      } else {
        STATE.ideas.unshift(newIdea);
      }
    } else {
      // Local mode
      newIdea.id = Date.now();
      STATE.ideas.unshift(newIdea);
      saveLocalIdeas();
    }

    showToast('⚡ Idea dumped into pipeline queue!', 'success');
    resetForm();
    renderApp();
  } catch (err) {
    console.error('Failed to submit idea:', err);
    showToast(`Error: ${err.message || 'Failed to save'}`, 'error');
  } finally {
    DOM.submitBtn.disabled = false;
  }
}

function resetForm() {
  DOM.ideaForm.reset();
  STATE.stagedUrls = [];
  STATE.stagedTags.clear();
  DOM.urlChips.innerHTML = '';
  DOM.activeTags.innerHTML = '';
  DOM.tagChips.forEach(chip => chip.classList.remove('active'));
}

function extractUrlsFromText(text) {
  const urlRegex = /(https?:\/\/[^\s]+)/g;
  const matches = text.match(urlRegex);
  return matches ? matches.map(u => u.replace(/[.,;:)]$/, '')) : [];
}

// -----------------------------------------------------------------------------
// Rendering & Feed
// -----------------------------------------------------------------------------
function renderApp() {
  renderMetrics();
  renderFeed();
}

function renderMetrics() {
  const total = STATE.ideas.length;
  const pending = STATE.ideas.filter(i => i.status === 'pending').length;
  const built = STATE.ideas.filter(i => i.status === 'built').length;

  DOM.statTotal.textContent = total;
  DOM.statPending.textContent = pending;
  DOM.statBuilt.textContent = built;
}

function renderFeed() {
  let filtered = STATE.ideas.slice();

  // Filter by status tab
  if (STATE.activeFilter !== 'all') {
    filtered = filtered.filter(i => (i.status || 'pending').toLowerCase() === STATE.activeFilter);
  }

  // Filter by search query
  if (STATE.searchQuery) {
    const q = STATE.searchQuery.toLowerCase();
    filtered = filtered.filter(i => {
      const titleMatch = (i.title || '').toLowerCase().includes(q);
      const contentMatch = (i.raw_content || '').toLowerCase().includes(q);
      const tagMatch = (i.tags || []).some(t => t.toLowerCase().includes(q));
      const urlMatch = (i.urls || []).some(u => u.toLowerCase().includes(q));
      return titleMatch || contentMatch || tagMatch || urlMatch;
    });
  }

  DOM.filteredCount.textContent = `${filtered.length} item${filtered.length === 1 ? '' : 's'}`;

  if (filtered.length === 0) {
    DOM.ideasFeed.innerHTML = `
      <div class="empty-state">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <circle cx="12" cy="12" r="10"></circle>
          <line x1="8" y1="12" x2="16" y2="12"></line>
        </svg>
        <p>No ideas matching current filter or search.</p>
        <button class="btn btn-secondary btn-sm" onclick="clearFilters()">Reset Filters</button>
      </div>
    `;
    return;
  }

  DOM.ideasFeed.innerHTML = filtered.map(idea => renderIdeaCard(idea)).join('');
}

function renderIdeaCard(idea) {
  const status = idea.status || 'pending';
  const statusClass = `status-${status.toLowerCase()}`;
  const statusLabel = status.toUpperCase();

  // Format date
  const createdDate = idea.created_at ? new Date(idea.created_at) : new Date();
  const dateFormatted = createdDate.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric'
  });
  const dayOfWeek = idea.day_of_week || createdDate.toLocaleDateString('en-US', { weekday: 'long' });

  // URLs
  const urls = idea.urls || [];
  const urlsHtml = urls.length ? `
    <div class="card-links-list">
      ${urls.map(url => `
        <a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" class="card-link-item" title="${escapeHtml(url)}">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
            <polyline points="15 3 21 3 21 9"></polyline>
            <line x1="10" y1="14" x2="21" y2="3"></line>
          </svg>
          <span>${cleanUrlDisplay(url)}</span>
        </a>
      `).join('')}
    </div>
  ` : '';

  // Tags
  const tags = idea.tags || [];
  const tagsHtml = tags.length ? `
    <div class="chips-container">
      ${tags.map(tag => `
        <span class="chip-item">#${escapeHtml(tag)}</span>
      `).join('')}
    </div>
  ` : '';

  // Built repo banner
  const builtBannerHtml = idea.github_repo_url ? `
    <div class="built-project-box">
      <div>
        <div class="built-title">✓ Shipped on GitHub</div>
        <div style="font-size: 0.75rem; color: var(--text-dim);">${escapeHtml(idea.github_repo_url)}</div>
      </div>
      <a href="${escapeHtml(idea.github_repo_url)}" target="_blank" rel="noopener noreferrer" class="built-btn">Open Repo →</a>
    </div>
  ` : '';

  return `
    <article class="idea-card" data-id="${idea.id}">
      <div class="idea-card-header">
        <h3 class="idea-title">${escapeHtml(idea.title || 'Untitled Idea')}</h3>
        <span class="status-badge ${statusClass}">${statusLabel}</span>
      </div>

      <div class="idea-meta">
        <span>${dayOfWeek}, ${dateFormatted}</span>
        <span>•</span>
        <span style="text-transform: capitalize;">Priority: ${escapeHtml(idea.priority || 'normal')}</span>
      </div>

      <div class="idea-body">${escapeHtml(idea.raw_content)}</div>

      ${urlsHtml}
      ${tagsHtml}
      ${builtBannerHtml}
    </article>
  `;
}

function cleanUrlDisplay(url) {
  try {
    const parsed = new URL(url);
    return (parsed.hostname.replace('www.', '') + parsed.pathname).substring(0, 32);
  } catch (e) {
    return url.substring(0, 32);
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

window.clearFilters = function() {
  STATE.activeFilter = 'all';
  STATE.searchQuery = '';
  DOM.searchInput.value = '';
  DOM.filterTabs.forEach(t => t.classList.toggle('active', t.dataset.filter === 'all'));
  renderFeed();
};

// -----------------------------------------------------------------------------
// Event Handlers & Micro-Interactions
// -----------------------------------------------------------------------------
function setupEventListeners() {
  // Form submit
  DOM.ideaForm.addEventListener('submit', handleIdeaSubmit);

  // Command + Enter shortcut
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      if (document.activeElement === DOM.ideaContent || document.activeElement === DOM.ideaTitle) {
        handleIdeaSubmit();
      }
    }
  });

  // Adding URLs manually
  DOM.addLinkBtn.addEventListener('click', addUrlFromInput);
  DOM.linkInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addUrlFromInput();
    }
  });

  // Quick tag chips
  DOM.tagChips.forEach(chip => {
    chip.addEventListener('click', () => {
      const tag = chip.dataset.tag;
      if (STATE.stagedTags.has(tag)) {
        STATE.stagedTags.delete(tag);
        chip.classList.remove('active');
      } else {
        STATE.stagedTags.add(tag);
        chip.classList.add('active');
      }
      renderActiveTags();
    });
  });

  // Custom tag
  DOM.customTagInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const val = DOM.customTagInput.value.trim().replace(/^#/, '');
      if (val) {
        STATE.stagedTags.add(val);
        DOM.customTagInput.value = '';
        renderActiveTags();
      }
    }
  });

  // Filter tabs
  DOM.filterTabs.forEach(tab => {
    tab.addEventListener('click', () => {
      DOM.filterTabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      STATE.activeFilter = tab.dataset.filter;
      renderFeed();
    });
  });

  // Search input
  DOM.searchInput.addEventListener('input', (e) => {
    STATE.searchQuery = e.target.value.trim();
    renderFeed();
  });

  // Modal Open/Close
  DOM.openConfigBtn.addEventListener('click', () => {
    DOM.configModal.classList.remove('hidden');
  });

  DOM.closeModalBtn.addEventListener('click', () => {
    DOM.configModal.classList.add('hidden');
  });

  DOM.configModal.addEventListener('click', (e) => {
    if (e.target === DOM.configModal) {
      DOM.configModal.classList.add('hidden');
    }
  });

  // Save Supabase Config
  DOM.saveConfigBtn.addEventListener('click', () => {
    const url = DOM.supabaseUrlInput.value.trim();
    const key = DOM.supabaseKeyInput.value.trim();

    if (!url || !key) {
      showToast('Please provide both Project URL and Anon Key', 'error');
      return;
    }

    try {
      if (window.supabase) {
        STATE.supabase = window.supabase.createClient(url, key);
        STATE.isDemoMode = false;
        localStorage.setItem(STATE.configKey, JSON.stringify({ url, key }));
        updateConnectionBadge(true);
        DOM.configModal.classList.add('hidden');
        showToast('Connected to Supabase PostgreSQL!', 'success');
        loadIdeas().then(renderApp);
      }
    } catch (err) {
      showToast(`Connection failed: ${err.message}`, 'error');
    }
  });

  // Clear config / Switch to Demo
  DOM.clearConfigBtn.addEventListener('click', () => {
    localStorage.setItem(STATE.configKey, JSON.stringify({ mode: 'demo' }));
    STATE.supabase = null;
    STATE.isDemoMode = true;
    updateConnectionBadge(false);
    DOM.supabaseUrlInput.value = '';
    DOM.supabaseKeyInput.value = '';
    DOM.configModal.classList.add('hidden');
    showToast('Switched to Demo / Local Storage mode', 'success');
    loadIdeas().then(renderApp);
  });
}

function addUrlFromInput() {
  const val = DOM.linkInput.value.trim();
  if (!val) return;

  let urlToAdd = val;
  if (!/^https?:\/\//i.test(urlToAdd)) {
    urlToAdd = 'https://' + urlToAdd;
  }

  try {
    new URL(urlToAdd);
    if (!STATE.stagedUrls.includes(urlToAdd)) {
      STATE.stagedUrls.push(urlToAdd);
      renderStagedUrls();
    }
    DOM.linkInput.value = '';
  } catch (err) {
    showToast('Please enter a valid URL', 'error');
  }
}

function renderStagedUrls() {
  DOM.urlChips.innerHTML = STATE.stagedUrls.map((url, idx) => `
    <span class="chip-item">
      <span>${cleanUrlDisplay(url)}</span>
      <button type="button" class="chip-remove" onclick="removeStagedUrl(${idx})">&times;</button>
    </span>
  `).join('');
}

window.removeStagedUrl = function(index) {
  STATE.stagedUrls.splice(index, 1);
  renderStagedUrls();
};

function renderActiveTags() {
  DOM.activeTags.innerHTML = Array.from(STATE.stagedTags).map(tag => `
    <span class="chip-item">
      <span>#${escapeHtml(tag)}</span>
      <button type="button" class="chip-remove" onclick="removeStagedTag('${escapeHtml(tag)}')">&times;</button>
    </span>
  `).join('');
}

window.removeStagedTag = function(tag) {
  STATE.stagedTags.delete(tag);
  DOM.tagChips.forEach(chip => {
    if (chip.dataset.tag === tag) chip.classList.remove('active');
  });
  renderActiveTags();
};

function showToast(message, type = 'success') {
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <span>${type === 'success' ? '✓' : '⚠'}</span>
    <span>${escapeHtml(message)}</span>
  `;
  DOM.toastContainer.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    setTimeout(() => toast.remove(), 200);
  }, 3200);
}

// Start
document.addEventListener('DOMContentLoaded', initApp);
