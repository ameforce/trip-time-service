(function () {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const accountButton = byId("account-toggle");
  const addButton = byId("favorites-add");
  const favoritesList = byId("favorites-list");
  const expandButton = byId("favorites-expand");
  const importButton = byId("favorites-import");
  const retryButton = byId("account-retry");
  const favoritesStatus = byId("favorites-status");
  const accountDialog = byId("account-dialog");
  const accountForm = byId("account-form");
  const accountStatus = byId("account-status");
  const accountSwitch = byId("account-switch");
  const username = byId("account-username");
  const password = byId("account-password");
  const accountManagement = byId("account-management");
  const deleteForm = byId("account-delete-form");
  const deleteUsername = byId("account-delete-username");
  const deletePassword = byId("account-delete-password");
  const favoriteDialog = byId("favorite-dialog");
  const favoriteForm = byId("favorites-add-form");
  const favoriteStatus = byId("favorite-status");
  const favoriteName = byId("fav-name");
  const favoriteSearch = byId("fav-address");
  const favoriteResults = byId("favorite-place-results");
  const favoriteSearchStatus = byId("favorite-search-status");
  const favoriteSaveButton = byId("fav-add-btn");
  const accountChannel = typeof BroadcastChannel === "function"
    ? new BroadcastChannel("trip-time-account")
    : null;
  const checkingSession = "계정 상태를 확인하고 있습니다.";
  const initialFavoriteCount = 3;

  // A null snapshot is unverified; a verified guest is a snapshot with user: null.
  let snapshot = null;
  let sessionRead = null;
  let sessionVersion = 0;
  let pendingWrite = false;
  let refreshAfterWrite = false;
  let mode = "login";
  let favoriteLookup = null;
  let chosenPlace = null;
  let renderedPlaces = null;
  let favoritesExpanded = false;
  const favoriteUseVersion = { origin: 0, dest: 0 };

  function activeUsername() {
    return snapshot?.user?.username ?? null;
  }

  function activeAccountId() {
    return snapshot?.user?.id ?? null;
  }

  function show(element, visible) {
    element.classList.toggle("hidden", !visible);
  }

  function status(element, message = "", isError = false) {
    element.textContent = message;
    element.classList.toggle("is-error", isError);
  }

  async function fetchJson(path, { controller = new AbortController(), timeout = 20000, ...options } = {}) {
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {
        credentials: "same-origin",
        cache: "no-store",
        ...options,
        signal: controller.signal,
      });
      const data = response.status === 204 ? null : await response.json().catch(() => null);
      return { response, data };
    } finally {
      clearTimeout(timer);
    }
  }

  async function request(path, { method = "GET", body, account, controller, timeout } = {}) {
    const headers = {};
    if (method !== "GET") headers["X-Requested-With"] = "TripTime";
    if (account) headers["X-TripTime-Account"] = account;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const { response, data } = await fetchJson(path, {
      method, headers, controller, timeout,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (response.status === 204) return null;
    if (!response.ok) {
      const error = new Error("Account request failed");
      error.status = response.status;
      error.reason = data?.reason || data?.detail?.reason || data?.detail?.code || data?.detail;
      throw error;
    }
    if (!data || !Array.isArray(data.favorites) ||
        (data.user !== null && (!data.user?.id || typeof data.user.id !== "string" ||
          typeof data.user.username !== "string"))) {
      throw new Error("Account response was incomplete");
    }
    return data;
  }

  function apiError(error, action) {
    if (error.status === 401 && action === "login") return "아이디 또는 비밀번호를 확인해 주세요.";
    if (error.status === 401 && action === "delete-account" && isWrongPassword(error)) {
      return "현재 비밀번호를 확인해 주세요.";
    }
    if (isAccountChanged(error)) return "로그인 상태가 바뀌었습니다. 다시 확인해 주세요.";
    if (error.status === 401 || error.status === 403) return "로그인 상태가 바뀌었습니다. 다시 확인해 주세요.";
    if (error.status === 429) return "요청이 많습니다. 잠시 뒤 다시 시도해 주세요.";
    if (error.status === 409 && action === "register") return "이미 사용 중인 아이디입니다.";
    if (error.status === 409 && /20개|limit|cap|full/i.test(String(error.reason))) {
      return "즐겨찾기 저장 한도에 도달했습니다. 기존 장소를 삭제한 뒤 다시 시도해 주세요.";
    }
    if (error.status === 409 && action === "add") return "이미 저장한 즐겨찾기 이름입니다.";
    if (error.status === 404 && action === "remove") return "이미 삭제된 즐겨찾기입니다.";
    if (error.status === 422 || error.status === 400) return "입력한 내용을 확인하고 다시 시도해 주세요.";
    return "요청을 처리하지 못했습니다. 연결 상태를 확인하고 다시 시도해 주세요.";
  }

  function isAccountChanged(error) {
    return error.status === 409 && /현재 계정|account.*chang/i.test(String(error.reason));
  }

  function isWrongPassword(error) {
    return /비밀번호|password|credential/i.test(String(error.reason));
  }

  function stopSessionRead() {
    sessionRead?.controller.abort();
    sessionRead = null;
  }

  function setSnapshot(next) {
    const previous = activeAccountId();
    snapshot = next;
    if (!snapshot || previous !== activeAccountId()) {
      sessionVersion += 1;
      favoritesExpanded = false;
      clearPrivateRouteState();
      closeDialog(favoriteDialog);
      closeDialog(accountDialog);
    }
    render();
    if (snapshot?.user === null) queueAutocompleteWarmup(() => snapshot?.user === null);
  }

  function refreshSession({ forget = false, quiet = false, fresh = false } = {}) {
    if (forget) {
      setSnapshot(null);
      status(favoritesStatus, checkingSession);
    }
    if (pendingWrite) {
      refreshAfterWrite = true;
      return;
    }
    if (sessionRead && !forget && !fresh) return sessionRead.promise;
    stopSessionRead();
    const controller = new AbortController();
    const read = { controller };
    sessionRead = read;
    const showRecovery = !retryButton.classList.contains("hidden");
    show(retryButton, false);
    if (!snapshot && (!quiet || showRecovery || !favoritesStatus.textContent)) {
      status(favoritesStatus, checkingSession);
    }
    read.promise = readSession(read, { quiet, showRecovery });
    return read.promise;
  }

  async function readSession(read, { quiet, showRecovery }) {
    try {
      const previous = activeAccountId();
      const next = await request("/api/auth/me", { controller: read.controller, timeout: 15000 });
      if (sessionRead !== read) return;
      if (!next) throw new Error("Account session response was empty");
      setSnapshot(next);
      if (previous && previous !== next.user?.id) {
        accountChannel?.postMessage("session-changed");
        status(favoritesStatus, next.user ? "로그인한 계정이 바뀌었습니다." :
          "로그인이 만료되었거나 다른 탭에서 로그아웃했습니다.");
      } else if (!quiet || showRecovery || favoritesStatus.textContent === checkingSession) {
        status(favoritesStatus);
      }
      return next;
    } catch (_) {
      if (sessionRead !== read) return;
      setSnapshot(null);
      status(favoritesStatus, "계정 상태를 확인하지 못했습니다. 다시 확인하면 즐겨찾기를 사용할 수 있습니다.", true);
      show(retryButton, true);
    } finally {
      if (sessionRead === read) sessionRead = null;
    }
  }

  async function write(path, { action, method = "POST", body, success, dialog, focusAfter }) {
    if (!snapshot || pendingWrite) return;
    const account = activeAccountId();
    const version = sessionVersion;
    const signsIn = action === "register" || action === "login";
    const signsOut = action === "logout" || action === "delete-account";
    const changesSession = signsIn || signsOut;
    const notice = dialog === accountDialog ? accountStatus : dialog === favoriteDialog ? favoriteStatus : favoritesStatus;
    const trigger = dialog === accountDialog ? accountButton : dialog === favoriteDialog ? addButton : document.activeElement;
    let succeeded = false;
    stopSessionRead();
    pendingWrite = true;
    status(notice);
    renderLocks();
    try {
      const result = await request(path, { method, body, account });
      if ((signsIn && !result?.user) || (signsOut && result !== null) ||
          (!changesSession && result?.user?.id !== account)) {
        throw new Error("Account response did not match");
      }
      if (changesSession) accountChannel?.postMessage("session-changed");
      else accountChannel?.postMessage("favorites-changed");
      if (version !== sessionVersion) return;
      setSnapshot(result);
      if (result === null) refreshAfterWrite = true;
      if (dialog) closeDialog(dialog);
      status(favoritesStatus, typeof success === "function" ? success(result) : success);
      succeeded = true;
    } catch (error) {
      if (version !== sessionVersion) return;
      const message = apiError(error, action);
      if (account && (isAccountChanged(error) ||
          ((error.status === 401 || error.status === 403) && !isWrongPassword(error)))) {
        accountChannel?.postMessage("session-changed");
        setSnapshot(null);
        refreshAfterWrite = true;
      } else if (!error.status || (action === "remove" && error.status === 404)) {
        refreshAfterWrite = true;
      }
      status(dialog?.open ? notice : favoritesStatus, message, true);
    } finally {
      if (dialog === accountDialog) {
        password.value = "";
        deletePassword.value = "";
      }
      pendingWrite = false;
      renderLocks();
      if (!dialog?.open && document.activeElement === document.body) {
        if (succeeded && focusAfter) focusAfter();
        else (trigger.isConnected ? trigger : addButton).focus({ preventScroll: true });
      }
      if (refreshAfterWrite) {
        refreshAfterWrite = false;
        const next = await refreshSession({ quiet: true });
        if (succeeded && dialog && document.activeElement === document.body) {
          (next ? trigger : retryButton).focus({ preventScroll: true });
        }
      }
    }
  }

  function createButton(text, handler, style = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "place-button" + (style ? " " + style : "");
    button.textContent = text;
    button.addEventListener("click", handler);
    return button;
  }

  function legacyPlaces() {
    return getFavorites().flatMap((favorite, index) => (
      typeof favorite?.name === "string" && typeof favorite?.address === "string"
        ? [{ favorite, index }]
        : []
    ));
  }

  function verifiedGuest(next) {
    return next?.user === null && next === snapshot && !pendingWrite;
  }

  async function applyFavorite(favorite, endpoint, account) {
    const useVersion = ++favoriteUseVersion[endpoint];
    if (!snapshot || account !== activeAccountId() || pendingWrite) return;
    const version = sessionVersion;
    const input = endpoint === "origin" ? $origin : $destination;
    const previousValue = input.value;
    const previousPlace = getSelectedState(endpoint);
    const verified = await refreshSession({ quiet: true });
    if (!verified || verified !== snapshot || input.value !== previousValue ||
        getSelectedState(endpoint) !== previousPlace) return;
    if (version !== sessionVersion || useVersion !== favoriteUseVersion[endpoint] || pendingWrite) return;
    if (!account) {
      if (verifiedGuest(verified)) useFavoriteForRoute(favorite, endpoint, false);
      return;
    }
    const current = verified.favorites.find((entry) => entry.id === favorite.id);
    if (current && account === activeAccountId()) {
      useFavoriteForRoute(current, endpoint, true);
      status(favoritesStatus, endpoint === "origin" ? "출발지에 추가했습니다." : "도착지에 추가했습니다.");
    } else {
      status(favoritesStatus, "이 즐겨찾기를 사용할 수 없습니다.", true);
    }
  }

  function focusAfterRemove(rowIndex) {
    const buttons = favoritesList.querySelectorAll(".favorite-remove");
    (buttons[Math.min(rowIndex, buttons.length - 1)] ?? addButton).focus({ preventScroll: true });
  }

  async function removeFavorite(favorite, index, account, rowIndex) {
    if (!snapshot || pendingWrite || account !== activeAccountId()) return;
    if (account) {
      void write("/api/favorites/" + favorite.id, {
        method: "DELETE",
        action: "remove",
        success: "즐겨찾기를 삭제했습니다.",
        focusAfter: () => focusAfterRemove(rowIndex),
      });
      return;
    }
    if (!verifiedGuest(await refreshSession({ quiet: true, fresh: true }))) return;
    const places = getFavorites();
    if (JSON.stringify(places[index]) !== JSON.stringify(favorite)) {
      render();
      return;
    }
    places.splice(index, 1);
    if (saveGuestPlaces(places)) {
      focusAfterRemove(rowIndex);
      status(favoritesStatus, "즐겨찾기를 삭제했습니다.");
    }
  }

  function render() {
    const account = activeAccountId();
    const guest = snapshot ? legacyPlaces() : [];
    const places = account ? snapshot.favorites.map((favorite) => ({ favorite })) : guest;
    const remaining = Math.max(0, places.length - initialFavoriteCount);
    const collapseFocused = remaining === 0 && document.activeElement === expandButton;
    if (!remaining) favoritesExpanded = false;
    const visiblePlaces = favoritesExpanded ? places : places.slice(0, initialFavoriteCount);
    accountButton.textContent = account ? "내 계정 · " + activeUsername() : "로그인";
    show(byId("favorites-empty"), Boolean(snapshot && places.length === 0));
    show(importButton, Boolean(account && guest.length));
    show(expandButton, remaining > 0);
    expandButton.textContent = favoritesExpanded ? "즐겨찾기 접기" : "즐겨찾기 " + remaining + "개 더 보기";
    expandButton.setAttribute("aria-expanded", String(favoritesExpanded));
    favoritesList.classList.toggle("is-expanded", favoritesExpanded);

    const nextPlaces = JSON.stringify([account, visiblePlaces]);
    if (nextPlaces !== renderedPlaces) {
      const focused = favoritesList.contains(document.activeElement);
      favoritesList.replaceChildren(...visiblePlaces.map(({ favorite, index }, rowIndex) => {
        const row = document.createElement("li");
        row.className = "fav-chip";
        const place = document.createElement("div");
        place.className = "fav-chip-place";
        place.id = "favorite-place-" + rowIndex;
        const name = document.createElement("strong");
        name.className = "fav-chip-name";
        name.textContent = favorite.name;
        const address = document.createElement("span");
        address.className = "fav-chip-address";
        address.textContent = favorite.address;
        place.append(name, address);
        const actions = document.createElement("div");
        actions.className = "favorites-actions";
        const buttons = [
          createButton("출발지", () => void applyFavorite(favorite, "origin", account)),
          createButton("도착지", () => void applyFavorite(favorite, "dest", account)),
          createButton("삭제", () => void removeFavorite(favorite, index, account, rowIndex),
            "place-button-danger favorite-remove"),
        ];
        for (const button of buttons) button.setAttribute("aria-describedby", place.id);
        actions.append(...buttons);
        row.append(place, actions);
        return row;
      }));
      renderedPlaces = nextPlaces;
      if (focused && !pendingWrite) addButton.focus({ preventScroll: true });
    }
    renderLocks();
    if (collapseFocused && snapshot) focusAfterRemove(initialFavoriteCount - 1);
  }

  function renderLocks() {
    const locked = !snapshot || pendingWrite;
    for (const button of [accountButton, addButton, importButton, expandButton,
      ...favoritesList.querySelectorAll("button"),
      ...accountDialog.querySelectorAll("button:not([data-close-dialog])")]) {
      button.disabled = locked;
    }
    retryButton.disabled = pendingWrite;
    favoritesList.setAttribute("aria-busy", String(locked));
    favoriteSaveButton.disabled = locked || !chosenPlace;
  }

  function closeDialog(dialog) {
    if (!dialog.open) return;
    dialog.close();
    if (dialog === accountDialog) {
      accountForm.reset();
      deleteForm.reset();
      accountButton.focus({ preventScroll: true });
    } else {
      cancelLookup();
      favoriteForm.reset();
      favoriteSearch.value = "";
      favoriteResults.replaceChildren();
      selectPlace(null);
      addButton.focus({ preventScroll: true });
    }
  }

  function showAccountMode(nextMode) {
    mode = nextMode;
    const authenticating = mode === "login" || mode === "register";
    const registering = mode === "register";
    show(accountForm, authenticating);
    show(accountManagement, mode === "manage");
    show(deleteForm, mode === "delete");
    const title = { login: "로그인", register: "회원가입", manage: "내 계정", delete: "계정 삭제" }[mode];
    byId("account-title").textContent = title;
    byId("account-submit").textContent = title;
    byId("account-identity").textContent = activeUsername() ?? "";
    deleteUsername.value = mode === "delete" ? activeUsername() ?? "" : "";
    deletePassword.value = "";
    password.value = "";
    password.autocomplete = registering ? "new-password" : "current-password";
    accountSwitch.textContent = registering ? "로그인" : "회원가입";
    show(byId("account-password-hint"), registering);
    if (registering) {
      password.setAttribute("aria-describedby", "account-password-hint");
    } else {
      password.removeAttribute("aria-describedby");
    }
    status(accountStatus);
  }

  function placeFrom(selection) {
    const coords = normalizeCoords(selection);
    const name = String(selection?.display_name || selection?.name || selection?.address || "").trim();
    const address = String(selection?.address || name).trim();
    if (!coords || Math.abs(coords.lat) > 90 || Math.abs(coords.lon) > 180 || !name || !address) return null;
    return { name, address, ...coords };
  }

  function placeTextKey(value) {
    return String(value || "").normalize("NFC").toLowerCase()
      .replace(/(?:특별자치시|특별자치도|특별시|광역시)(?=\s|$)/g, "")
      .replace(/[\s,()]/g, "");
  }

  function geocodeCandidateQuery(candidate, candidates) {
    const queryKey = placeTextKey(candidate.canonical_query);
    const sharedQuery = candidates.some((other) => other !== candidate &&
      placeTextKey(other.canonical_query) === queryKey);
    if (!sharedQuery && queryKey !== placeTextKey(candidate.display_name)) return candidate.canonical_query;
    const addressKey = placeTextKey(candidate.address);
    const distinctAddress = addressKey !== placeTextKey(candidate.display_name) &&
      !candidates.some((other) => other !== candidate && placeTextKey(other.address) === addressKey);
    if (distinctAddress) return candidate.address;
    return sharedQuery ? "" : candidate.canonical_query;
  }

  function candidateCoords(result, candidate, query) {
    const coords = normalizeCoords(result);
    if (!coords || Math.abs(coords.lat) > 90 || Math.abs(coords.lon) > 180) return null;
    const expectedAddress = placeTextKey(candidate.address);
    const expectedName = placeTextKey(candidate.display_name);
    const displayName = String(result.display_name || "").trim();
    const combined = displayName.match(/^(.+)\s+\(([^()]+)\)$/);
    const structuredAddress = placeTextKey(result.address);
    const displayedAddress = combined ? placeTextKey(combined[2]) : "";
    const hasAddressSuffix = combined && (displayedAddress === structuredAddress ||
      displayedAddress === expectedAddress || trimToCoreRoadAddress(combined[2]));
    const actualAddress = structuredAddress || (hasAddressSuffix ? displayedAddress : "");
    const isAddress = candidate.selection_kind === "address";
    if (actualAddress && (isAddress || expectedAddress !== expectedName)) {
      return actualAddress === expectedAddress ? coords : null;
    }
    if (isAddress) return placeTextKey(displayName) === expectedAddress ? coords : null;
    const names = [placeTextKey(displayName)];
    if (hasAddressSuffix) names.push(placeTextKey(combined[1]));
    const expectedLabels = [expectedName, expectedAddress, placeTextKey(query)];
    if (displayName && !names.some((name) => expectedLabels.includes(name))) return null;
    return coords;
  }

  async function chooseSearchResult(candidate, candidates) {
    if (!snapshot || !favoriteDialog.open || pendingWrite) return;
    const readyPlace = placeFrom(candidate);
    if (readyPlace) {
      choosePlace(readyPlace);
      return;
    }
    cancelLookup();
    selectPlace(null);
    const query = geocodeCandidateQuery(candidate, candidates);
    if (!query) {
      status(favoriteSearchStatus, "같은 이름의 장소를 구분할 수 없습니다. 주소로 다시 검색해 주세요.", true);
      return;
    }
    const controller = new AbortController();
    favoriteLookup = controller;
    status(favoriteSearchStatus, "선택한 장소의 위치를 확인하고 있습니다.");
    try {
      const { response, data: results } = await fetchJson("/api/geocode?q=" + encodeURIComponent(query) + buildMapCenterQueryString(), {
        controller,
        timeout: AUTOCOMPLETE_FETCH_TIMEOUT_MS,
      });
      if (!response.ok) throw new Error("Place coordinates could not be loaded");
      if (favoriteLookup !== controller || !favoriteDialog.open) return;
      const coords = results.map((result) => candidateCoords(result, candidate, query)).find(Boolean);
      const resolved = coords && placeFrom({ ...candidate, ...coords });
      if (!resolved) throw new Error("Place coordinates did not match");
      choosePlace(resolved);
    } catch (_) {
      if (favoriteLookup === controller) {
        status(favoriteSearchStatus, "선택한 장소의 위치를 확인하지 못했습니다. 다시 선택하거나 주소로 검색해 주세요.", true);
      }
    } finally {
      if (favoriteLookup === controller) favoriteLookup = null;
    }
  }

  function selectPlace(place) {
    chosenPlace = place;
    const selected = byId("favorite-selected-place");
    show(selected, Boolean(place));
    selected.textContent = place ? place.name + " · " + place.address : "";
    renderLocks();
  }

  function choosePlace(place) {
    if (!snapshot || !favoriteDialog.open || !place || pendingWrite) return;
    cancelLookup();
    favoriteResults.replaceChildren();
    status(favoriteSearchStatus);
    status(favoriteStatus);
    selectPlace(place);
    if (!favoriteName.value.trim()) {
      favoriteName.value = Array.from(place.name).slice(0, favoriteName.maxLength).join("");
    }
    favoriteName.focus();
  }

  function cancelLookup() {
    favoriteLookup?.abort();
    favoriteLookup = null;
  }

  function saveGuestPlaces(places, notice = favoritesStatus) {
    try {
      localStorage.setItem(FAV_KEY, JSON.stringify(places));
      render();
      return true;
    } catch (_) {
      status(notice, "이 기기에 즐겨찾기를 저장할 수 없습니다.", true);
      return false;
    }
  }

  [accountDialog, favoriteDialog].forEach((dialog) => {
    dialog.querySelector("[data-close-dialog]").addEventListener("click", () => closeDialog(dialog));
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeDialog(dialog);
    });
  });

  accountButton.addEventListener("click", () => {
    if (!snapshot || pendingWrite) return;
    showAccountMode(activeAccountId() ? "manage" : "login");
    accountDialog.showModal();
  });

  accountSwitch.addEventListener("click", () => {
    showAccountMode(mode === "login" ? "register" : "login");
    username.focus();
  });

  accountForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!snapshot || activeAccountId()) return;
    const name = username.value.normalize("NFC");
    if (!/^[가-힣A-Za-z0-9_]{3,32}$/.test(name)) {
      status(accountStatus, "아이디는 한글, 영문, 숫자, 밑줄로 3~32자 입력해 주세요.", true);
      username.focus();
      return;
    }
    const length = Array.from(password.value).length;
    if (length < 15 || length > 128) {
      status(accountStatus, "비밀번호는 15~128자로 입력해 주세요.", true);
      password.focus();
      return;
    }
    void write("/api/auth/" + mode, {
      action: mode,
      body: { username: name, password: password.value },
      success: mode === "register" ? "가입했습니다. 계정에 즐겨찾기를 저장할 수 있습니다." : "로그인했습니다.",
      dialog: accountDialog,
    });
  });

  byId("account-logout").addEventListener("click", () => {
    if (!activeAccountId()) return;
    void write("/api/auth/logout", {
      action: "logout",
      success: "로그아웃했습니다.",
      dialog: accountDialog,
    });
  });
  byId("account-delete-start").addEventListener("click", () => {
    showAccountMode("delete");
    deletePassword.focus();
  });
  byId("account-delete-cancel").addEventListener("click", () => {
    showAccountMode("manage");
    byId("account-delete-start").focus();
  });
  deleteForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!activeAccountId()) return;
    void write("/api/auth/account", {
      method: "DELETE",
      action: "delete-account",
      body: { password: deletePassword.value },
      success: "계정과 즐겨찾기를 삭제했습니다.",
      dialog: accountDialog,
    });
  });

  addButton.addEventListener("click", () => {
    if (!snapshot || pendingWrite) return;
    const origin = placeFrom(getSelectedState("origin"));
    const destination = placeFrom(getSelectedState("dest"));
    show(byId("favorite-current-places"), Boolean(origin || destination));
    show(byId("favorite-use-origin"), Boolean(origin));
    show(byId("favorite-use-destination"), Boolean(destination));
    status(favoriteSearchStatus);
    status(favoriteStatus);
    favoriteDialog.showModal();
  });
  byId("favorite-use-origin").addEventListener("click", () => choosePlace(placeFrom(getSelectedState("origin"))));
  byId("favorite-use-destination").addEventListener("click", () => choosePlace(placeFrom(getSelectedState("dest"))));
  favoriteSearch.addEventListener("input", () => {
    cancelLookup();
    favoriteResults.replaceChildren();
    selectPlace(null);
    status(favoriteSearchStatus);
  });
  byId("favorite-place-search").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!snapshot || pendingWrite) return;
    cancelLookup();
    favoriteResults.replaceChildren();
    selectPlace(null);
    const query = favoriteSearch.value.trim();
    if (query.length < AUTOCOMPLETE_MIN_QUERY_LENGTH) {
      status(favoriteSearchStatus, "장소 이름이나 주소를 두 글자 이상 입력해 주세요.", true);
      return;
    }
    const controller = new AbortController();
    favoriteLookup = controller;
    status(favoriteSearchStatus, "장소를 찾고 있습니다.");
    try {
      const { response, data: items } = await fetchJson("/api/autocomplete?q=" + encodeURIComponent(query) + buildMapCenterQueryString(), {
        controller,
        timeout: AUTOCOMPLETE_FETCH_TIMEOUT_MS,
      });
      if (!response.ok) throw new Error("Place lookup failed");
      if (favoriteLookup !== controller || !favoriteDialog.open) return;
      const places = items.map((item) => buildStableSelection(item, {
        selection_kind: item?.address?.trim() ? "" : "poi",
      })).filter(Boolean);
      for (const place of places) {
        const row = document.createElement("li");
        const label = place.display_name === place.address ? place.display_name : place.display_name + " · " + place.address;
        row.append(createButton(label, () => void chooseSearchResult(place, places)));
        favoriteResults.append(row);
      }
      status(favoriteSearchStatus, places.length ? "저장할 장소를 선택해 주세요." :
        "장소를 찾지 못했습니다. 다른 이름이나 주소로 검색해 주세요.");
    } catch (_) {
      if (favoriteLookup === controller) {
        status(favoriteSearchStatus, "장소를 검색하지 못했습니다. 다시 시도해 주세요.", true);
      }
    } finally {
      if (favoriteLookup === controller) favoriteLookup = null;
    }
  });

  favoriteForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!snapshot || pendingWrite || !chosenPlace) return;
    const name = favoriteName.value.trim();
    if (!name) {
      status(favoriteStatus, "즐겨찾기 이름을 입력해 주세요.", true);
      favoriteName.focus();
      return;
    }
    if (Array.from(chosenPlace.address).length > 100) {
      status(favoriteStatus, "주소는 100자까지 저장할 수 있습니다. 다른 장소를 선택해 주세요.", true);
      return;
    }
    const selection = chosenPlace;
    const favorite = { ...selection, name };
    if (activeAccountId()) {
      void write("/api/favorites", {
        action: "add",
        body: favorite,
        success: "즐겨찾기를 저장했습니다.",
        dialog: favoriteDialog,
      });
      return;
    }
    if (!verifiedGuest(await refreshSession({ quiet: true, fresh: true })) ||
        !favoriteDialog.open || chosenPlace !== selection || favoriteName.value.trim() !== name) return;
    const guest = getFavorites();
    if (guest.length >= FAV_MAX) {
      status(favoriteStatus, "이 기기에 즐겨찾기를 최대 " + FAV_MAX + "개 저장할 수 있습니다.", true);
    } else if (guest.some((entry) => entry?.name === name)) {
      status(favoriteStatus, "이미 이 기기에 저장한 즐겨찾기 이름입니다.", true);
    } else if (saveGuestPlaces([...guest, favorite], favoriteStatus)) {
      closeDialog(favoriteDialog);
      status(favoritesStatus, "이 기기에 즐겨찾기를 저장했습니다.");
    }
  });

  importButton.addEventListener("click", () => {
    if (!activeAccountId()) return;
    const legacy = getFavorites();
    const favorites = legacy.map(placeFrom).filter((place) => (
      place && Array.from(place.name).length <= 20 && Array.from(place.address).length <= 100
    )).slice(0, FAV_MAX);
    if (!favorites.length) {
      status(favoritesStatus, "이 기기에 좌표가 확인된 즐겨찾기가 없습니다.", true);
      return;
    }
    void write("/api/favorites/import", {
      action: "import",
      body: { favorites },
      success: (result) => result.import.imported + "개를 가져왔습니다. " +
        (result.import.skipped + legacy.length - favorites.length) + "개는 건너뛰었습니다.",
    });
  });

  expandButton.addEventListener("click", () => {
    favoritesExpanded = !favoritesExpanded;
    render();
  });
  retryButton.addEventListener("click", async () => {
    await refreshSession();
    if (document.activeElement === document.body) {
      (snapshot ? accountButton : retryButton).focus({ preventScroll: true });
    }
  });
  accountChannel?.addEventListener("message", (event) => {
    if (event.data === "session-changed") {
      void refreshSession({ forget: true });
    } else if (event.data === "favorites-changed" && activeAccountId()) {
      stopSessionRead();
      void refreshSession({ quiet: true });
    }
  });
  window.addEventListener("storage", (event) => {
    if (event.key === FAV_KEY || event.key === null) render();
  });
  window.addEventListener("focus", () => void refreshSession({ quiet: true }));
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) void refreshSession({ forget: true });
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") void refreshSession({ quiet: true });
  });
  window.setInterval(() => {
    if (activeAccountId() && document.visibilityState === "visible") void refreshSession({ quiet: true });
  }, 60000);

  render();
  void refreshSession();
})();
