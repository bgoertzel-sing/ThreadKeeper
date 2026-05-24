;Configured at runtime memory and history in amount of characters:
(= (maxFeedback) (empty))
(= (maxRecallItems) (empty))
(= (maxEpisodeRecallLines) (empty))
(= (maxHistory) (empty))
(= (embeddingprovider) (empty))

(= (initMemory)
   (progn (println! "Initializing memory")
          (configure maxFeedback 50000)
          (configure maxRecallItems 20)
          (configure maxEpisodeRecallLines 20)
          (configure maxHistory 30000)
          (configure embeddingprovider Local) ;OpenAI or Local
          (if (== (embeddingprovider) Local)
              (py-call (lib_llm_ext.initLocalEmbedding)) _)))

(= (getPrompt)
   (read-file (py-call (risk_register.prompt_file))))

(= (getHistory)
   ;; Resync 2026-05-23 — Python-shim path. helper.history_with_snapshot
   ;; reads the byte-tail of memory/history.metta (Python port of
   ;; upstream src/skills.pl:read_file_tail/3) and prepends
   ;; risk_register.context_snapshot(). Returns the same
   ;; "CONTEXT_SNAPSHOT: <snap> RECENT_HISTORY: <tail>" shape the
   ;; fork's original MeTTa getHistory produced, without depending on
   ;; upstream's prolog read_file_tail predicate. The fork already
   ;; routes getPrompt through risk_register via py-call, so this is
   ;; idiomatic — not a special case.
   (py-call (helper.history_with_snapshot (maxHistory))))

(= (addToHistory $lastmessage $response $sexpr $msgnew)
   (if $msgnew
       (if (== (get-state &error) ())
           (appendToHistory ((get_time_as_string) (newline) "HUMAN_MESSAGE: " $lastmessage (newline) $response (newline)))
           (appendToHistory ((get_time_as_string) (newline) "HUMAN_MESSAGE: " $lastmessage (newline) $response (newline) ERROR_FEEDBACK: (get-state &error))))
       (if (== (get-state &error) ())
           (appendToHistory ((get_time_as_string) (newline) $response (newline)))
           (appendToHistory ((get_time_as_string) (newline) $response (newline) ERROR_FEEDBACK: (get-state &error))))))

(= (embed $str)
   (if (== (embeddingprovider) Local)
       (py-call (lib_llm_ext.useLocalEmbedding (string-safe $str)))
       (useGPTEmbedding (string-safe $str))))

(= (appendToHistory $addition)
   (append-file (library OmegaClaw-Core ./memory/history.metta) (swrite $addition)))

(= (remember $str)
   (progn (py-call (lib_chromadb.remember $str (embed $str) (get_time_as_string)))
          REMEMBER-SUCCESS))

(= (query $str)
   (py-call (lib_chromadb.query (embed $str) (maxRecallItems))))

(= (episodes $time)
   (py-call (helper.around_time $time (maxEpisodeRecallLines))))
