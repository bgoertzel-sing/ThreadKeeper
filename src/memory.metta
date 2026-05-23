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
   ;; Resync 2026-05-23: adopt upstream's MemoryLeakFix wholesale.
   ;;
   ;; Initial hybrid attempt wrapped read_file_tail inside a let* to combine
   ;; with the fork's risk_register.context_snapshot prepend, but read_file_tail
   ;; is a prolog-imported function — it doesn't evaluate as a normal MeTTa
   ;; function inside let*. The let* bound the literal unevaluated atom
   ;; `(read_file_tail <path> <cap>)` to $tail, so the prompt contained the
   ;; function-call shape instead of the file content. Caught by the post-rebase
   ;; §4.11 test (parent input collapsed from 15K → 2.7K tokens because
   ;; HISTORY was effectively empty).
   ;;
   ;; The fork's CONTEXT_SNAPSHOT feature has been temporarily dropped from
   ;; getHistory. To re-add it post-rebase, define a separate prolog-side
   ;; combinator or write a small helper that calls read_file_tail via
   ;; translatePredicate. Tracked as a follow-up.
   (read_file_tail (library OmegaClaw-Core ./memory/history.metta) (maxHistory)))

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
