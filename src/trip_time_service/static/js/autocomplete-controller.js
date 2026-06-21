(function () {
  "use strict";

  var METRIC_MAX_EVENTS = 240;
  var metrics = {
    counters: {},
    events: [],
    last: {},
  };

  function normalizeAutocompleteQuery(value) {
    var text = String(value || "").trim();
    if (!text) return "";
    try {
      return text.normalize("NFC");
    } catch (_) {
      return text;
    }
  }

  function hasTrailingHangulJamoNfc(value) {
    var normalized = normalizeAutocompleteQuery(value);
    return /[\u1100-\u11FF\u3130-\u318F\uA960-\uA97F\uD7B0-\uD7FF]$/.test(normalized);
  }

  function isHangulJamoChar(value) {
    return /^[\u1100-\u11FF\u3130-\u318F\uA960-\uA97F\uD7B0-\uD7FF]$/.test(value || "");
  }

  function stripTrailingHangulJamoCluster(value) {
    var chars = Array.from(normalizeAutocompleteQuery(value));
    while (chars.length && isHangulJamoChar(chars[chars.length - 1])) {
      chars.pop();
    }
    return normalizeAutocompleteQuery(chars.join(""));
  }

  function queryClass(query) {
    var text = String(query || "");
    var hasHangulSyllable = /[\uAC00-\uD7A3]/.test(text);
    var hasJamo = /[\u1100-\u11FF\u3130-\u318F\uA960-\uA97F\uD7B0-\uD7FF]/.test(text);
    var hasAscii = /[A-Za-z]/.test(text);
    var hasDigit = /\d/.test(text);
    var hasRoadToken = /(번길|대로|로|길|번지)/.test(text);
    if (hasRoadToken || hasDigit) return hasHangulSyllable ? "road_address" : "numeric";
    if (hasHangulSyllable && hasJamo) return "hangul_mixed_jamo";
    if (hasHangulSyllable) return "hangul";
    if (hasJamo) return "jamo_tail";
    if (hasAscii) return "ascii";
    return text ? "other" : "empty";
  }

  function queryLengthBucket(query) {
    var compactLength = String(query || "").replace(/\s+/g, "").length;
    if (compactLength <= 0) return "0";
    if (compactLength === 1) return "1";
    if (compactLength === 2) return "2";
    if (compactLength <= 4) return "3-4";
    if (compactLength <= 8) return "5-8";
    return "9+";
  }

  function queryHash(query) {
    var text = String(query || "");
    var hash = 2166136261;
    for (var i = 0; i < text.length; i++) {
      hash ^= text.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(16);
  }

  function metricPayload(query, extra) {
    var normalized = normalizeAutocompleteQuery(query);
    return Object.assign(
      {
        query_hash: queryHash(normalized),
        query_length_bucket: queryLengthBucket(normalized),
        query_class: queryClass(normalized),
        trailing_jamo: hasTrailingHangulJamoNfc(normalized),
      },
      extra || {}
    );
  }

  function record(timerKey, stage, detail) {
    var now = Date.now();
    var perfNow =
      typeof performance !== "undefined" && typeof performance.now === "function"
        ? Math.round(performance.now())
        : now;
    var safeDetail = Object.assign({}, detail || {});
    delete safeDetail.query;
    delete safeDetail.raw_query;
    delete safeDetail.value;
    var event = Object.assign(
      {
        ts: now,
        t: perfNow,
        input: timerKey,
        stage: stage,
      },
      safeDetail
    );
    metrics.events.push(event);
    if (metrics.events.length > METRIC_MAX_EVENTS) {
      metrics.events.splice(0, metrics.events.length - METRIC_MAX_EVENTS);
    }
    var counterKey = stage + (safeDetail.reason ? ":" + safeDetail.reason : "");
    metrics.counters[counterKey] = (metrics.counters[counterKey] || 0) + 1;
    metrics.last[timerKey] = event;
  }

  function publishMetricsForDebug() {
    window.__ttsAutocompleteMetrics = metrics;
    window.__ttsGetAutocompleteMetrics = function () {
      return JSON.parse(JSON.stringify(metrics));
    };
  }

  function fetchAutocompleteInstrumented(query, abortState, timerKey, requestMeta) {
    var cacheKey = buildAutocompleteCacheKey(query);
    var cachedItems = getCachedAutocompleteItems(cacheKey);
    if (Array.isArray(cachedItems) && cachedItems.length > 0) {
      record(
        timerKey,
        "cache_hit",
        metricPayload(query, {
          seq: requestMeta && requestMeta.seq,
          count: cachedItems.length,
        })
      );
      return Promise.resolve(cachedItems);
    }

    if (abortState.ctrl) {
      record(
        timerKey,
        "abort",
        metricPayload(query, {
          seq: requestMeta && requestMeta.seq,
          reason: "new-request",
        })
      );
      abortState.ctrl.abort();
    }
    abortState.ctrl = new AbortController();
    var requestStartedAt =
      typeof performance !== "undefined" && typeof performance.now === "function"
        ? performance.now()
        : Date.now();
    var timeoutId = setTimeout(function () {
      if (abortState.ctrl) {
        record(
          timerKey,
          "abort",
          metricPayload(query, {
            seq: requestMeta && requestMeta.seq,
            reason: "timeout",
          })
        );
        abortState.ctrl.abort();
      }
    }, AUTOCOMPLETE_FETCH_TIMEOUT_MS);
    return fetch(
      "/api/autocomplete?q=" +
        encodeURIComponent(query) +
        buildMapCenterQueryString(),
      {
        signal: abortState.ctrl.signal,
        credentials: "same-origin",
        cache: "no-store",
      }
    )
      .then(function (r) {
        if (!r.ok) throw new Error("autocomplete " + r.status);
        return r.json();
      })
      .then(function (items) {
        var durationMs = Math.round(
          (typeof performance !== "undefined" && typeof performance.now === "function"
            ? performance.now()
            : Date.now()) - requestStartedAt
        );
        if (!Array.isArray(items)) {
          record(
            timerKey,
            "response",
            metricPayload(query, {
              seq: requestMeta && requestMeta.seq,
              reason: "non-array",
              duration_ms: durationMs,
            })
          );
          return [];
        }
        setCachedAutocompleteItems(cacheKey, items);
        record(
          timerKey,
          "response",
          metricPayload(query, {
            seq: requestMeta && requestMeta.seq,
            count: items.length,
            duration_ms: durationMs,
          })
        );
        return items;
      })
      .catch(function (err) {
        record(
          timerKey,
          "response_error",
          metricPayload(query, {
            seq: requestMeta && requestMeta.seq,
            reason: err && err.name === "AbortError" ? "abort" : "error",
            duration_ms: Math.round(
              (typeof performance !== "undefined" && typeof performance.now === "function"
                ? performance.now()
                : Date.now()) - requestStartedAt
            ),
          })
        );
        return [];
      })
      .finally(function () {
        clearTimeout(timeoutId);
      });
  }

  function setupImeAutocompleteController($input, $dropdown, setSelected, timerKey) {
    if (!$input || !$dropdown) return;
    var currentItems = [];
    var activeIdx = -1;
    var abortState = { ctrl: null };
    var requestSeq = 0;
    var state = {
      phase: "idle",
      composing: false,
      lastScheduledQuery: "",
      lastRenderedQuery: "",
      lastStableQuery: "",
    };

    function resetActiveIndex() {
      activeIdx = -1;
      _acActiveIdx[timerKey] = -1;
    }

    function transition(nextPhase, query, detail) {
      state.phase = nextPhase;
      record(
        timerKey,
        "state",
        metricPayload(query, Object.assign({ phase: nextPhase }, detail || {}))
      );
    }

    function deriveEffectiveQuery(value) {
      var displayQuery = normalizeAutocompleteQuery(value);
      var trailingJamo = hasTrailingHangulJamoNfc(displayQuery);
      if (!trailingJamo) {
        return {
          displayQuery: displayQuery,
          effectiveQuery: displayQuery,
          trailingJamo: false,
          source: "display",
        };
      }
      var stablePrefix = stripTrailingHangulJamoCluster(displayQuery);
      if (!stablePrefix && state.lastStableQuery) {
        stablePrefix = state.lastStableQuery;
      }
      return {
        displayQuery: displayQuery,
        effectiveQuery: stablePrefix,
        trailingJamo: true,
        source: stablePrefix ? "stable-prefix" : "none",
      };
    }

    function handleInput(event, reason) {
      clearAutocompleteTimer(timerKey);
      var derived = deriveEffectiveQuery($input.value);
      var displayQuery = derived.displayQuery;
      var q = derived.effectiveQuery;
      var eventIsComposing = !!(
        event &&
        (event.isComposing ||
          (event.inputType && String(event.inputType).indexOf("Composition") >= 0))
      );
      record(
        timerKey,
        "input",
        metricPayload(q, {
          reason: reason || "input",
          composing: state.composing || eventIsComposing,
          input_type: event && event.inputType ? String(event.inputType) : "",
          effective_query_source: derived.source,
          display_trailing_jamo: derived.trailingJamo,
        })
      );
      if (shouldRetainSelectionOnInput(timerKey, displayQuery)) {
        closeACDropdown($dropdown);
        transition("selected", displayQuery, { reason: "retain-selection" });
        return;
      }
      setSelected(null);
      if (typeof invalidateRouteInputState === "function") {
        invalidateRouteInputState();
      }
      if (!derived.trailingJamo && q.length >= AUTOCOMPLETE_MIN_QUERY_LENGTH) {
        state.lastStableQuery = q;
      }
      if (derived.trailingJamo && q.length < AUTOCOMPLETE_MIN_QUERY_LENGTH) {
        transition("composing", displayQuery, {
          reason: "trailing-jamo-no-stable-prefix",
          effective_query_source: derived.source,
        });
        return;
      }
      if (abortState.ctrl) {
        record(timerKey, "abort", metricPayload(q, { reason: "input-change" }));
        abortState.ctrl.abort();
        abortState.ctrl = null;
      }
      if (q.length < AUTOCOMPLETE_MIN_QUERY_LENGTH) {
        closeACDropdown($dropdown);
        currentItems = [];
        resetActiveIndex();
        transition("idle", q, { reason: "below-min-length" });
        return;
      }

      var timerId = setTimeout(function () {
        requestSeq += 1;
        var seq = requestSeq;
        var queryAtRequest = q;
        state.lastScheduledQuery = queryAtRequest;
        transition("loading", queryAtRequest, {
          seq: seq,
          effective_query_source: derived.source,
          display_trailing_jamo: derived.trailingJamo,
        });
        fetchAutocompleteInstrumented(queryAtRequest, abortState, timerKey, { seq: seq }).then(
          function (items) {
            if (seq !== requestSeq) {
              record(timerKey, "stale_drop", metricPayload(queryAtRequest, { seq: seq, reason: "seq" }));
              return;
            }
            var currentDerived = deriveEffectiveQuery($input.value);
            if (currentDerived.effectiveQuery !== queryAtRequest) {
              record(timerKey, "stale_drop", metricPayload(queryAtRequest, { seq: seq, reason: "query" }));
              return;
            }
            if (!items || !items.length) {
              if (
                currentDerived.trailingJamo &&
                currentItems.length > 0 &&
                state.lastRenderedQuery === queryAtRequest
              ) {
                transition("rendered", queryAtRequest, {
                  seq: seq,
                  reason: "retain-last-good-empty",
                  count: currentItems.length,
                });
                return;
              }
              currentItems = [];
              resetActiveIndex();
              closeACDropdown($dropdown);
              transition("empty", queryAtRequest, { seq: seq });
              return;
            }
            currentItems = items;
            resetActiveIndex();
            renderACDropdown($dropdown, items, function (item) {
              applyAutocompleteSelection($input, $dropdown, setSelected, item);
            });
            state.lastRenderedQuery = queryAtRequest;
            transition("rendered", queryAtRequest, {
              seq: seq,
              count: items.length,
              coords_ready_count: items.filter(function (item) {
                return item && item.coords_ready === true;
              }).length,
            });
          }
        );
      }, AUTOCOMPLETE_DEBOUNCE_MS);
      transition("scheduled", q, {
        reason: reason || "input",
        debounce_ms: AUTOCOMPLETE_DEBOUNCE_MS,
        effective_query_source: derived.source,
        display_trailing_jamo: derived.trailingJamo,
      });
      setAutocompleteTimer(timerKey, timerId);
    }

    function handleFocusRetry() {
      var derived = deriveEffectiveQuery($input.value);
      var q = derived.effectiveQuery;
      if (q.length < AUTOCOMPLETE_MIN_QUERY_LENGTH) return;
      if (state.phase === "loading" || state.phase === "scheduled") return;
      if (currentItems.length > 0 && state.lastRenderedQuery === q) {
        renderACDropdown($dropdown, currentItems, function (item) {
          applyAutocompleteSelection($input, $dropdown, setSelected, item);
        });
        transition("rendered", q, {
          reason: "focus-restore",
          count: currentItems.length,
        });
        return;
      }
      handleInput(null, "focus-retry");
    }

    $input.addEventListener(
      "input",
      function (event) {
        event.stopImmediatePropagation();
        handleInput(event, "input");
      },
      true
    );

    $input.addEventListener(
      "compositionstart",
      function (event) {
        event.stopImmediatePropagation();
        state.composing = true;
        transition("composing", normalizeAutocompleteQuery($input.value), {
          reason: "compositionstart",
        });
      },
      true
    );

    $input.addEventListener(
      "compositionend",
      function (event) {
        event.stopImmediatePropagation();
        state.composing = false;
        record(timerKey, "composition_end", metricPayload($input.value, { reason: "compositionend" }));
        setTimeout(function () {
          handleInput(null, "compositionend");
        }, 0);
      },
      true
    );

    $input.addEventListener(
      "focus",
      function () {
        setTimeout(handleFocusRetry, 0);
      },
      true
    );

    $input.addEventListener(
      "keydown",
      function (event) {
        var items = $dropdown.querySelectorAll(".ac-item");
        if (!items.length) return;
        if (event.key === "ArrowDown") {
          event.preventDefault();
          event.stopImmediatePropagation();
          activeIdx = Math.min(activeIdx + 1, items.length - 1);
          _acActiveIdx[timerKey] = activeIdx;
          items.forEach(function (el, i) {
            el.classList.toggle("ac-active", i === activeIdx);
          });
        } else if (event.key === "ArrowUp") {
          event.preventDefault();
          event.stopImmediatePropagation();
          activeIdx = Math.max(activeIdx - 1, 0);
          _acActiveIdx[timerKey] = activeIdx;
          items.forEach(function (el, i) {
            el.classList.toggle("ac-active", i === activeIdx);
          });
        } else if (event.key === "Enter" && activeIdx >= 0 && currentItems[activeIdx]) {
          event.preventDefault();
          event.stopImmediatePropagation();
          applyAutocompleteSelection($input, $dropdown, setSelected, currentItems[activeIdx]);
        } else if (event.key === "Escape") {
          event.stopImmediatePropagation();
          closeACDropdown($dropdown);
          resetActiveIndex();
        }
      },
      true
    );

    $input.addEventListener(
      "blur",
      function (event) {
        event.stopImmediatePropagation();
        clearAutocompleteTimer(timerKey);
        record(
          timerKey,
          "blur_close",
          metricPayload($input.value, {
            delay_ms: 150,
          })
        );
        setTimeout(function () {
          closeACDropdown($dropdown);
          resetActiveIndex();
        }, 150);
      },
      true
    );
  }

  publishMetricsForDebug();
  window.__ttsAutocompleteControllerVersion = "v2-single-owner";
  setupImeAutocompleteController($origin, $originAC, function (v) {
    _selectedOrigin = v;
  }, "origin");
  setupImeAutocompleteController($destination, $destAC, function (v) {
    _selectedDest = v;
  }, "dest");
})();
