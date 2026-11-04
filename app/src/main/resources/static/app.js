import { h, render } from 'preact';
import { useEffect, useRef, useState } from 'preact/hooks';
import htm from 'htm';

const html = htm.bind(h);

class ApiError extends Error {
  constructor(status, body) {
    super(`API ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;
  const isBinaryBlob = typeof Blob !== 'undefined' && options.body instanceof Blob;
  if (!isFormData && !isBinaryBlob && !headers.has('Content-Type') && options.body) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(path, {
    ...options,
    headers,
    credentials: 'include',
  });

  let data = null;
  try {
    data = await response.json();
  } catch (_) {
    data = null;
  }

  if (!response.ok) {
    throw new ApiError(response.status, data);
  }

  return data;
}

function parseHash() {
  const raw = window.location.hash.replace(/^#/, '') || '/decks';
  const [pathPart, queryPart = ''] = raw.split('?');
  const parts = pathPart.split('/').filter(Boolean);
  const query = new URLSearchParams(queryPart);

  if (parts[0] === 'login') {
    return { name: 'login', query };
  }
  if (parts[0] === 'review') {
    return { name: 'review', query };
  }
  if (parts[0] === 'manage') {
    return { name: 'manage', query };
  }
  if (parts[0] === 'card' && parts[1]) {
    return { name: 'card', cardId: Number(parts[1]), query };
  }
  return { name: 'decks', query };
}

function toMediaUrl(path) {
  if (!path) return '';
  if (path.startsWith('/')) return path;
  return `/media/${path}`;
}

function normalizeCard(card) {
  return {
    ...card,
    tags: Array.isArray(card?.tags) ? card.tags : [],
    audioVariants: Array.isArray(card?.audioVariants) ? card.audioVariants : [],
  };
}

function formatDate(iso) {
  if (!iso) return '';
  let date = null;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (match) {
    const [, y, m, d] = match;
    date = new Date(Date.UTC(Number(y), Number(m) - 1, Number(d)));
  } else {
    const parsed = new Date(iso);
    if (!Number.isNaN(parsed.getTime())) {
      date = parsed;
    }
  }
  if (!date) return iso;
  return new Intl.DateTimeFormat('pl-PL', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(date);
}

function apiErrorMessage(err, fallback) {
  if (!(err instanceof ApiError)) return fallback;
  const detail = err.body && typeof err.body.error === 'string' ? err.body.error : '';
  return detail ? `${fallback} (${detail})` : fallback;
}

function emptyCardDraft() {
  return {
    type: 'WORD',
    frontTextPl: '',
    frontTranscript: '',
    backRuTranslation: '',
    backRuPronHint: '',
    dateIso: '',
    imagePath: '',
    historyAnswerPl: '',
    historyAnswerRu: '',
    tagsCsv: '',
  };
}

function historyAnswers(card) {
  const legacy = card.backRuTranslation || '';
  const legacyIsRussian = /[А-Яа-яЁё]/u.test(legacy);
  return {
    pl: card.historyAnswerPl || (legacyIsRussian ? '' : legacy),
    ru: card.historyAnswerRu || (legacyIsRussian ? legacy : ''),
  };
}

function cardToDraft(card) {
  return {
    type: card.type || 'WORD',
    frontTextPl: card.frontTextPl || '',
    frontTranscript: card.frontTranscript || '',
    backRuTranslation: card.backRuTranslation || '',
    backRuPronHint: card.backRuPronHint || '',
    dateIso: card.dateIso || '',
    imagePath: card.imagePath || '',
    historyAnswerPl: card.type === 'HISTORY_FACT' ? historyAnswers(card).pl : '',
    historyAnswerRu: card.type === 'HISTORY_FACT' ? historyAnswers(card).ru : '',
    tagsCsv: Array.isArray(card.tags) ? card.tags.join(', ') : '',
  };
}

function draftToPayload(draft) {
  const tags = (draft.tagsCsv || '')
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean);

  const isHistory = draft.type === 'HISTORY_FACT';
  const backRuTranslation = isHistory ? (draft.historyAnswerRu || draft.historyAnswerPl || '') : draft.backRuTranslation;

  return {
    type: draft.type,
    frontTextPl: draft.frontTextPl,
    frontTranscript: draft.frontTranscript,
    backRuTranslation,
    backRuPronHint: isHistory ? '' : draft.backRuPronHint,
    dateIso: draft.dateIso,
    imagePath: draft.imagePath,
    historyAnswerPl: isHistory ? draft.historyAnswerPl : '',
    historyAnswerRu: isHistory ? draft.historyAnswerRu : '',
    tags,
  };
}

function App() {
  const [route, setRoute] = useState(parseHash());
  const [me, setMe] = useState(null);
  const [checkingAuth, setCheckingAuth] = useState(true);

  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  async function loadMe() {
    try {
      const user = await api('/api/me');
      setMe(user);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setMe(null);
      } else {
        console.error(err);
      }
    } finally {
      setCheckingAuth(false);
    }
  }

  useEffect(() => {
    loadMe();
  }, []);

  async function logout() {
    try {
      await fetch('/auth/logout', { method: 'POST', credentials: 'include' });
    } finally {
      setMe(null);
      window.location.hash = '/login';
    }
  }

  if (checkingAuth) {
    return html`<div class="panel">Loading…</div>`;
  }

  if (!me || route.name === 'login') {
    return html`<${LoginScreen} />`;
  }

  return html`
    <div>
      <header class="topbar">
        <div class="brand">Kotland</div>
        <div class="row-wrap">
          <button class="btn-secondary" onClick=${() => (window.location.hash = '/decks')}>Decks</button>
          <button class="btn-secondary" onClick=${() => (window.location.hash = '/manage')}>Manage</button>
          <button class="btn-secondary" onClick=${logout}>Logout</button>
        </div>
      </header>

      ${route.name === 'decks' && html`<${DecksScreen} />`}
      ${route.name === 'review' && html`<${ReviewScreen} deckCode=${route.query.get('deck') || ''} />`}
      ${route.name === 'manage' && html`<${ManageScreen} />`}
      ${route.name === 'card' && html`<${CardDetailScreen} cardId=${route.cardId} />`}
    </div>
  `;
}

function LoginScreen() {
  return html`
    <section class="panel">
      <h1>Polish cards for Russian speakers</h1>
      <p class="muted">Google sign-in is required.</p>
      <button onClick=${() => (window.location.href = '/auth/login')}>Sign in with Google</button>
    </section>
  `;
}

function DecksScreen() {
  const [decks, setDecks] = useState([]);
  const [error, setError] = useState('');
  const [busyDeckCode, setBusyDeckCode] = useState('');

  async function loadDecks() {
    try {
      const items = await api('/api/decks');
      setDecks(items);
    } catch (err) {
      setError('Failed to load decks.');
    }
  }

  useEffect(() => {
    loadDecks();
  }, []);

  async function resetAndStart(deck) {
    if (!deck || busyDeckCode) return;
    const confirmed = window.confirm(`Reset progress for "${deck.title}" and start now?`);
    if (!confirmed) return;

    setBusyDeckCode(deck.code);
    setError('');
    try {
      await api(`/api/decks/${encodeURIComponent(deck.code)}/reset`, { method: 'POST' });
      await loadDecks();
      window.location.hash = `/review?deck=${encodeURIComponent(deck.code)}`;
    } catch (err) {
      setError(apiErrorMessage(err, 'Failed to reset deck.'));
    } finally {
      setBusyDeckCode('');
    }
  }

  return html`
    <section class="panel">
      <h1>Decks</h1>
      <p class="muted">Choose a deck and start review.</p>
      ${error && html`<p>${error}</p>`}
      <div class="deck-list">
        ${decks.map(
          (deck) => html`
            <div class="panel deck-row" key=${deck.id}>
              <div>
                <div><strong>${deck.title}</strong></div>
                <div class="muted">Code: ${deck.code}</div>
              </div>
              <div class="row-wrap">
                <span class="tag">Due ${deck.dueCount}</span>
                <button onClick=${() => (window.location.hash = `/review?deck=${encodeURIComponent(deck.code)}`)}>Start</button>
                <button
                  class="btn-secondary"
                  disabled=${Boolean(busyDeckCode)}
                  onClick=${() => resetAndStart(deck)}
                >
                  ${busyDeckCode === deck.code ? 'Resetting…' : 'Reset & Start'}
                </button>
              </div>
            </div>
          `,
        )}
      </div>
    </section>
  `;
}

function ReviewScreen({ deckCode }) {
  const [queue, setQueue] = useState([]);
  const [queueError, setQueueError] = useState('');
  const [index, setIndex] = useState(0);
  const [showBack, setShowBack] = useState(false);
  const [selectedAudioPath, setSelectedAudioPath] = useState('');
  const [startedAt, setStartedAt] = useState(Date.now());
  const [isSubmitting, setIsSubmitting] = useState(false);
  const audioRef = useRef(null);

  const current = queue[index] || null;

  async function loadQueue() {
    setQueueError('');
    try {
      const query = new URLSearchParams();
      if (deckCode) query.set('deck', deckCode);
      query.set('limit', '20');
      const data = await api(`/api/review/next?${query.toString()}`);
      const normalized = Array.isArray(data?.cards) ? data.cards.map(normalizeCard) : [];
      setQueue(normalized);
      setIndex(0);
    } catch (err) {
      setQueueError('Failed to load review queue.');
      setQueue([]);
    }
  }

  useEffect(() => {
    loadQueue();
  }, [deckCode]);

  useEffect(() => {
    setShowBack(false);
    setStartedAt(Date.now());
    setSelectedAudioPath(current?.audioVariants?.[0]?.path || '');
  }, [current?.id]);

  useEffect(() => {
    if (!selectedAudioPath || !audioRef.current) return;
    audioRef.current.load();
    audioRef.current.play().catch(() => {});
  }, [selectedAudioPath]);

  async function answer(grade) {
    if (!current || isSubmitting) return;
    setIsSubmitting(true);
    try {
      await api('/api/review/answer', {
        method: 'POST',
        body: JSON.stringify({
          cardId: current.id,
          grade,
          responseMs: Date.now() - startedAt,
        }),
      });

      const nextIdx = index + 1;
      if (nextIdx >= queue.length) {
        await loadQueue();
      } else {
        setIndex(nextIdx);
      }
    } catch (err) {
      setQueueError('Failed to submit grade.');
    } finally {
      setIsSubmitting(false);
    }
  }

  if (queueError) {
    return html`<section class="panel">${queueError}</section>`;
  }

  if (!current) {
    return html`
      <section class="panel">
        <h1>Queue complete</h1>
        <p class="muted">No due cards right now.</p>
        <div class="row-wrap">
          <button onClick=${loadQueue}>Refresh</button>
          <button class="btn-secondary" onClick=${() => (window.location.hash = '/decks')}>Back to decks</button>
        </div>
      </section>
    `;
  }

  const audioVariants = current.audioVariants || [];
  const tags = current.tags || [];
  const selectedVariant = audioVariants.find((v) => v.path === selectedAudioPath) || audioVariants[0];
  const isHistory = current.type === 'HISTORY_FACT';
  const { pl: historyAnswerPl, ru: historyAnswerRu } = historyAnswers(current);

  return html`
    <section class="panel card-main">
      <div class="row">
        <span class="tag">${current.type}</span>
        <span class="spacer"></span>
        <button class="btn-secondary" onClick=${() => (window.location.hash = `/card/${current.id}`)}>Details</button>
      </div>

      <div>
        <div class="card-side-title">Polish</div>
        <div class="word">${current.frontTextPl}</div>
        ${current.frontTranscript && html`<div class="transcript">${current.frontTranscript}</div>`}
      </div>

      ${current.dateIso && html`<div class="tag">Date: ${formatDate(current.dateIso)}</div>`}
      ${current.imagePath &&
      html`<img
        key=${`review-image-${current.id}-${current.imagePath}`}
        class="card-image"
        src=${toMediaUrl(current.imagePath)}
        alt="Card image"
      />`}

      ${audioVariants.length > 0 &&
      html`
        <div>
          <div class="audio-chips">
            ${audioVariants.map(
              (audio) => html`
                <button
                  key=${audio.id}
                  class=${`chip ${audio.path === selectedVariant?.path ? 'active' : ''}`}
                  onClick=${() => setSelectedAudioPath(audio.path)}
                >
                  ${audio.label}
                </button>
              `,
            )}
          </div>
          <audio ref=${audioRef} controls preload="metadata" src=${toMediaUrl(selectedVariant?.path)}></audio>
        </div>
      `}

      <div>
        <button onClick=${() => setShowBack((v) => !v)}>${showBack ? 'Hide answer' : 'Show answer'}</button>
      </div>

      ${showBack &&
      html`
        <div class="answer">
          ${isHistory
            ? html`
                ${historyAnswerPl && html`
                  <div class="card-side-title">Answer (Polish)</div>
                  <div class="word" lang="pl">${historyAnswerPl}</div>
                `}
                ${historyAnswerRu &&
                html`
                  <div class="card-side-title" style="margin-top:8px;">Answer (Russian)</div>
                  <div class="transcript" lang="ru">${historyAnswerRu}</div>
                `}
              `
            : html`
                <div class="card-side-title">Russian</div>
                <div class="word">${current.backRuTranslation}</div>
                ${current.backRuPronHint && html`<div class="transcript">${current.backRuPronHint}</div>`}
              `}
          <div>
            ${tags.map((tag) => html`<span class="tag" key=${tag}>${tag}</span>`)}
          </div>
        </div>
      `}
    </section>

    <div class="bottom-actions">
      <div class="grade-grid">
        <button class="grade-again" onClick=${() => answer('AGAIN')} disabled=${!showBack || isSubmitting}>Again</button>
        <button class="grade-hard" onClick=${() => answer('HARD')} disabled=${!showBack || isSubmitting}>Hard</button>
        <button class="grade-good" onClick=${() => answer('GOOD')} disabled=${!showBack || isSubmitting}>Good</button>
        <button class="grade-easy" onClick=${() => answer('EASY')} disabled=${!showBack || isSubmitting}>Easy</button>
      </div>
    </div>
  `;
}

function CardDetailScreen({ cardId }) {
  const [card, setCard] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    (async () => {
      try {
        setError('');
        const loaded = await api(`/api/cards/${cardId}`);
        setCard(normalizeCard(loaded));
      } catch (err) {
        setError('Failed to load card.');
      }
    })();
  }, [cardId]);

  if (error) {
    return html`<section class="panel">${error}</section>`;
  }

  if (!card) {
    return html`<section class="panel">Loading…</section>`;
  }

  return html`
    <section class="panel">
      <div class="row-wrap">
        <button class="btn-secondary" onClick=${() => history.back()}>Back</button>
        <button class="btn-secondary" onClick=${() => (window.location.hash = '/decks')}>Decks</button>
      </div>
      <h1 style="margin-top:10px;">Card #${card.id}</h1>
      <p><strong>Type:</strong> ${card.type}</p>
      <p><strong>Polish:</strong> ${card.frontTextPl}</p>
      ${card.frontTranscript && html`<p><strong>Transcript:</strong> ${card.frontTranscript}</p>`}
      ${card.type === 'HISTORY_FACT'
        ? html`
            ${historyAnswers(card).pl && html`<p lang="pl"><strong>Answer (Polish):</strong> ${historyAnswers(card).pl}</p>`}
            ${historyAnswers(card).ru && html`<p lang="ru"><strong>Answer (Russian):</strong> ${historyAnswers(card).ru}</p>`}
          `
        : html`
            <p><strong>Russian:</strong> ${card.backRuTranslation}</p>
            ${card.backRuPronHint && html`<p><strong>Pronunciation hint:</strong> ${card.backRuPronHint}</p>`}
          `}
      ${card.dateIso && html`<p><strong>Date:</strong> ${formatDate(card.dateIso)}</p>`}
      ${card.imagePath &&
      html`<img
        key=${`detail-image-${card.id}-${card.imagePath}`}
        class="card-image"
        src=${toMediaUrl(card.imagePath)}
        alt="Card image"
      />`}

      <h3 style="margin-top:14px;">Audio Sources</h3>
      <div class="audio-chips">
        ${card.audioVariants.map(
          (audio) => html`<span class="chip" key=${audio.id}>${audio.label}</span>`,
        )}
      </div>
    </section>
  `;
}

function ManageScreen() {
  const [decks, setDecks] = useState([]);
  const [decksError, setDecksError] = useState('');
  const [selectedDeckId, setSelectedDeckId] = useState(null);
  const [newDeckCode, setNewDeckCode] = useState('');
  const [newDeckTitle, setNewDeckTitle] = useState('');
  const [editingDeckId, setEditingDeckId] = useState(null);
  const [editingDeckCode, setEditingDeckCode] = useState('');
  const [editingDeckTitle, setEditingDeckTitle] = useState('');
  const [cards, setCards] = useState([]);
  const [cardsError, setCardsError] = useState('');
  const [editingCardId, setEditingCardId] = useState(null);
  const [cardDraft, setCardDraft] = useState(emptyCardDraft());
  const [imageSearchQuery, setImageSearchQuery] = useState('');
  const [imageSearchResults, setImageSearchResults] = useState([]);
  const [imageSearchError, setImageSearchError] = useState('');
  const [busy, setBusy] = useState(false);

  const selectedDeck = decks.find((deck) => deck.id === selectedDeckId) || null;

  async function loadDecks(preferredDeckId = selectedDeckId) {
    setDecksError('');
    try {
      const loaded = await api('/api/manage/decks');
      setDecks(loaded);

      const hasPreferred = loaded.some((deck) => deck.id === preferredDeckId);
      if (hasPreferred) {
        setSelectedDeckId(preferredDeckId);
      } else {
        const preferred = loaded.find((deck) => deck.canEdit) || loaded[0] || null;
        setSelectedDeckId(preferred ? preferred.id : null);
      }
    } catch (err) {
      setDecksError(apiErrorMessage(err, 'Failed to load decks.'));
      setDecks([]);
      setSelectedDeckId(null);
    }
  }

  async function loadCards(deckId) {
    if (!deckId) {
      setCards([]);
      return;
    }
    setCardsError('');
    try {
      const loaded = await api(`/api/manage/decks/${deckId}/cards`);
      setCards(Array.isArray(loaded) ? loaded : []);
    } catch (err) {
      setCardsError(apiErrorMessage(err, 'Failed to load cards.'));
      setCards([]);
    }
  }

  useEffect(() => {
    loadDecks();
  }, []);

  useEffect(() => {
    loadCards(selectedDeckId);
    setEditingCardId(null);
    setCardDraft(emptyCardDraft());
    setImageSearchResults([]);
    setImageSearchError('');
  }, [selectedDeckId]);

  async function createDeck(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      const created = await api('/api/manage/decks', {
        method: 'POST',
        body: JSON.stringify({
          code: newDeckCode,
          title: newDeckTitle,
        }),
      });
      setNewDeckCode('');
      setNewDeckTitle('');
      await loadDecks(created.id);
    } catch (err) {
      setDecksError(apiErrorMessage(err, 'Failed to create deck.'));
    } finally {
      setBusy(false);
    }
  }

  function beginEditDeck(deck) {
    setEditingDeckId(deck.id);
    setEditingDeckCode(deck.code);
    setEditingDeckTitle(deck.title);
  }

  function cancelEditDeck() {
    setEditingDeckId(null);
    setEditingDeckCode('');
    setEditingDeckTitle('');
  }

  async function saveDeck(event) {
    event.preventDefault();
    if (!editingDeckId || busy) return;
    setBusy(true);
    try {
      await api(`/api/manage/decks/${editingDeckId}`, {
        method: 'PUT',
        body: JSON.stringify({
          code: editingDeckCode,
          title: editingDeckTitle,
        }),
      });
      await loadDecks(editingDeckId);
      cancelEditDeck();
    } catch (err) {
      setDecksError(apiErrorMessage(err, 'Failed to update deck.'));
    } finally {
      setBusy(false);
    }
  }

  async function removeDeck(deck) {
    if (!deck?.canEdit || busy) return;
    const ok = window.confirm(`Delete deck "${deck.title}" and all its cards?`);
    if (!ok) return;
    setBusy(true);
    try {
      await api(`/api/manage/decks/${deck.id}`, { method: 'DELETE' });
      await loadDecks(deck.id === selectedDeckId ? null : selectedDeckId);
      if (deck.id === selectedDeckId) {
        setCards([]);
      }
    } catch (err) {
      setDecksError(apiErrorMessage(err, 'Failed to delete deck.'));
    } finally {
      setBusy(false);
    }
  }

  function beginEditCard(card) {
    setEditingCardId(card.id);
    setCardDraft(cardToDraft(card));
  }

  function cancelEditCard() {
    setEditingCardId(null);
    setCardDraft(emptyCardDraft());
    setImageSearchResults([]);
    setImageSearchError('');
  }

  async function uploadImageFile(file) {
    if (!file || !selectedDeck?.canEdit || busy) return;
    setBusy(true);
    setImageSearchError('');
    try {
      const uploaded = await api('/api/manage/media/upload', {
        method: 'POST',
        headers: {
          'X-File-Name': file.name || 'upload.jpg',
        },
        body: file,
      });
      setCardDraft((prev) => ({ ...prev, imagePath: uploaded.path || '' }));
    } catch (err) {
      setImageSearchError(apiErrorMessage(err, 'Failed to upload image.'));
    } finally {
      setBusy(false);
    }
  }

  async function searchImages(event) {
    event?.preventDefault?.();
    if (!imageSearchQuery.trim()) {
      setImageSearchResults([]);
      return;
    }
    setBusy(true);
    setImageSearchError('');
    try {
      const query = new URLSearchParams({
        q: imageSearchQuery.trim(),
        limit: '12',
      });
      const results = await api(`/api/manage/images/search?${query.toString()}`);
      setImageSearchResults(Array.isArray(results) ? results : []);
    } catch (err) {
      setImageSearchError(apiErrorMessage(err, 'Failed to search images.'));
      setImageSearchResults([]);
    } finally {
      setBusy(false);
    }
  }

  async function importSearchImage(url) {
    if (!url || !selectedDeck?.canEdit || busy) return;
    setBusy(true);
    setImageSearchError('');
    try {
      const imported = await api('/api/manage/images/import', {
        method: 'POST',
        body: JSON.stringify({ url }),
      });
      setCardDraft((prev) => ({ ...prev, imagePath: imported.path || '' }));
    } catch (err) {
      setImageSearchError(apiErrorMessage(err, 'Failed to import image.'));
    } finally {
      setBusy(false);
    }
  }

  async function saveCard(event) {
    event.preventDefault();
    if (!selectedDeck || !selectedDeck.canEdit || busy) return;
    setBusy(true);
    try {
      const payload = draftToPayload(cardDraft);
      if (editingCardId) {
        await api(`/api/manage/cards/${editingCardId}`, {
          method: 'PUT',
          body: JSON.stringify(payload),
        });
      } else {
        await api(`/api/manage/decks/${selectedDeck.id}/cards`, {
          method: 'POST',
          body: JSON.stringify(payload),
        });
      }
      await loadCards(selectedDeck.id);
      await loadDecks(selectedDeck.id);
      cancelEditCard();
    } catch (err) {
      setCardsError(apiErrorMessage(err, 'Failed to save card.'));
    } finally {
      setBusy(false);
    }
  }

  async function removeCard(card) {
    if (!selectedDeck?.canEdit || busy) return;
    const ok = window.confirm('Delete this card?');
    if (!ok) return;
    setBusy(true);
    try {
      await api(`/api/manage/cards/${card.id}`, { method: 'DELETE' });
      await loadCards(selectedDeck.id);
      await loadDecks(selectedDeck.id);
      if (editingCardId === card.id) {
        cancelEditCard();
      }
    } catch (err) {
      setCardsError(apiErrorMessage(err, 'Failed to delete card.'));
    } finally {
      setBusy(false);
    }
  }

  return html`
    <section class="panel">
      <h1>Manage Decks</h1>
      <p class="muted">Create your own decks and edit cards directly in the app.</p>
      ${decksError && html`<p>${decksError}</p>`}
      <form class="manage-grid" onSubmit=${createDeck}>
        <input
          placeholder="code (latin, digits, dash)"
          value=${newDeckCode}
          onInput=${(e) => setNewDeckCode(e.currentTarget.value)}
          required
        />
        <input
          placeholder="title"
          value=${newDeckTitle}
          onInput=${(e) => setNewDeckTitle(e.currentTarget.value)}
          required
        />
        <button disabled=${busy}>Create Deck</button>
      </form>
    </section>

    <section class="panel">
      <h2>Your Accessible Decks</h2>
      <div class="deck-list">
        ${decks.map(
          (deck) => html`
            <div class="panel deck-row ${deck.id === selectedDeckId ? 'deck-selected' : ''}" key=${deck.id}>
              <div>
                <div><strong>${deck.title}</strong></div>
                <div class="muted">Code: ${deck.code}</div>
                <div class="row-wrap">
                  <span class="tag">Due ${deck.dueCount}</span>
                  <span class="tag">Cards ${deck.cardCount}</span>
                  ${deck.isSystem ? html`<span class="tag">System</span>` : html`<span class="tag">Custom</span>`}
                  ${deck.canEdit ? html`<span class="tag">Editable</span>` : html`<span class="tag">Read-only</span>`}
                </div>
              </div>
              <div class="row-wrap">
                <button class="btn-secondary" onClick=${() => (window.location.hash = `/review?deck=${encodeURIComponent(deck.code)}`)}>
                  Review
                </button>
                <button class="btn-secondary" onClick=${() => setSelectedDeckId(deck.id)}>Open</button>
                ${deck.canEdit &&
                html`
                  <button class="btn-secondary" onClick=${() => beginEditDeck(deck)}>Rename</button>
                  <button class="btn-secondary" onClick=${() => removeDeck(deck)}>Delete</button>
                `}
              </div>
            </div>
          `,
        )}
      </div>
    </section>

    ${editingDeckId &&
    html`
      <section class="panel">
        <h2>Edit Deck</h2>
        <form class="manage-grid" onSubmit=${saveDeck}>
          <input value=${editingDeckCode} onInput=${(e) => setEditingDeckCode(e.currentTarget.value)} required />
          <input value=${editingDeckTitle} onInput=${(e) => setEditingDeckTitle(e.currentTarget.value)} required />
          <div class="row-wrap">
            <button disabled=${busy}>Save</button>
            <button type="button" class="btn-secondary" onClick=${cancelEditDeck}>Cancel</button>
          </div>
        </form>
      </section>
    `}

    ${selectedDeck &&
    html`
      <section class="panel">
        <div class="row-wrap">
          <h2>${selectedDeck.title}</h2>
          <span class="tag">${selectedDeck.code}</span>
        </div>
        ${cardsError && html`<p>${cardsError}</p>`}
        ${selectedDeck.canEdit
          ? html`
              <form class="manage-card-form" onSubmit=${saveCard}>
                <div class="row-wrap">
                  <select
                    value=${cardDraft.type}
                    onChange=${(e) => setCardDraft({ ...cardDraft, type: e.currentTarget.value })}
                  >
                    <option value="WORD">WORD</option>
                    <option value="PHRASE">PHRASE</option>
                    <option value="HISTORY_FACT">HISTORY_FACT</option>
                  </select>
                  <input
                    class="wide"
                    placeholder=${cardDraft.type === 'HISTORY_FACT' ? 'Question in Polish' : 'Polish text'}
                    value=${cardDraft.frontTextPl}
                    onInput=${(e) => setCardDraft({ ...cardDraft, frontTextPl: e.currentTarget.value })}
                    required
                  />
                </div>
                <input
                  placeholder="Polish transcript (optional)"
                  value=${cardDraft.frontTranscript}
                  onInput=${(e) => setCardDraft({ ...cardDraft, frontTranscript: e.currentTarget.value })}
                />
                ${cardDraft.type === 'HISTORY_FACT'
                  ? html`
                      <input
                        placeholder="Answer in Polish"
                        value=${cardDraft.historyAnswerPl}
                        onInput=${(e) => setCardDraft({ ...cardDraft, historyAnswerPl: e.currentTarget.value })}
                        required
                      />
                      <input
                        placeholder="Answer in Russian (optional)"
                        value=${cardDraft.historyAnswerRu}
                        onInput=${(e) => setCardDraft({ ...cardDraft, historyAnswerRu: e.currentTarget.value })}
                      />
                    `
                  : html`
                      <input
                        placeholder="Russian translation"
                        value=${cardDraft.backRuTranslation}
                        onInput=${(e) => setCardDraft({ ...cardDraft, backRuTranslation: e.currentTarget.value })}
                        required
                      />
                      <input
                        placeholder="Russian pronunciation hint (optional)"
                        value=${cardDraft.backRuPronHint}
                        onInput=${(e) => setCardDraft({ ...cardDraft, backRuPronHint: e.currentTarget.value })}
                      />
                    `}
                <div class="row-wrap">
                  <input
                    type="date"
                    placeholder="Date (optional)"
                    value=${cardDraft.dateIso}
                    onInput=${(e) => setCardDraft({ ...cardDraft, dateIso: e.currentTarget.value })}
                  />
                  <input
                    class="wide"
                    placeholder="Image path under media root (optional)"
                    value=${cardDraft.imagePath}
                    onInput=${(e) => setCardDraft({ ...cardDraft, imagePath: e.currentTarget.value })}
                  />
                </div>
                <div class="row-wrap">
                  <input
                    class="wide"
                    type="file"
                    accept="image/*"
                    onChange=${(e) => uploadImageFile(e.currentTarget.files?.[0])}
                  />
                </div>
                <div class="row-wrap">
                  <input
                    class="wide"
                    placeholder="Search Wikimedia images"
                    value=${imageSearchQuery}
                    onInput=${(e) => setImageSearchQuery(e.currentTarget.value)}
                  />
                  <button type="button" class="btn-secondary" onClick=${searchImages} disabled=${busy}>Search</button>
                </div>
                ${imageSearchError && html`<p>${imageSearchError}</p>`}
                ${imageSearchResults.length > 0 &&
                html`
                  <div class="image-search-grid">
                    ${imageSearchResults.map(
                      (img) => html`
                        <div class="panel image-result" key=${img.sourceUrl}>
                          <img class="search-thumb" src=${img.thumbUrl} alt=${img.title} />
                          <div class="muted">${img.title}</div>
                          <div class="row-wrap">
                            <a class="btn-secondary" href=${img.pageUrl} target="_blank" rel="noreferrer">Source</a>
                            <button type="button" class="btn-secondary" onClick=${() => importSearchImage(img.sourceUrl)}>
                              Use
                            </button>
                          </div>
                        </div>
                      `,
                    )}
                  </div>
                `}
                <input
                  placeholder="Tags CSV (optional)"
                  value=${cardDraft.tagsCsv}
                  onInput=${(e) => setCardDraft({ ...cardDraft, tagsCsv: e.currentTarget.value })}
                />
                <div class="row-wrap">
                  <button disabled=${busy}>${editingCardId ? 'Update Card' : 'Add Card'}</button>
                  ${editingCardId &&
                  html`<button type="button" class="btn-secondary" onClick=${cancelEditCard}>Cancel Edit</button>`}
                </div>
              </form>
            `
          : html`<p class="muted">This deck is read-only.</p>`}
      </section>

      <section class="panel">
        <h2>Cards (${cards.length})</h2>
        ${cards.length === 0 && html`<p class="muted">No cards in this deck yet.</p>`}
        <div class="deck-list">
          ${cards.map(
            (card) => html`
              <div class="panel" key=${card.id}>
                <div class="row-wrap">
                  <span class="tag">${card.type}</span>
                  <span class="tag">#${card.id}</span>
                  ${card.dateIso && html`<span class="tag">${formatDate(card.dateIso)}</span>`}
                </div>
                <div><strong>${card.frontTextPl}</strong></div>
                <div class="muted">
                  ${card.type === 'HISTORY_FACT'
                    ? (card.historyAnswerPl || card.backRuTranslation || 'No answer')
                    : card.backRuTranslation}
                </div>
                ${card.imagePath && html`<div class="muted">Image: ${card.imagePath}</div>`}
                <div class="row-wrap">
                  <button class="btn-secondary" onClick=${() => (window.location.hash = `/card/${card.id}`)}>Open</button>
                  ${selectedDeck.canEdit &&
                  html`
                    <button class="btn-secondary" onClick=${() => beginEditCard(card)}>Edit</button>
                    <button class="btn-secondary" onClick=${() => removeCard(card)}>Delete</button>
                  `}
                </div>
              </div>
            `,
          )}
        </div>
      </section>
    `}
  `;
}

render(html`<${App} />`, document.getElementById('app'));
