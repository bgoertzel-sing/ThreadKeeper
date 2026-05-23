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
   ;; Hybrid resolution (resync 2026-05-23): adopt upstream's MemoryLeakFix
   ;; (read_file_tail instead of full read + last_chars) AND keep the fork's
   ;; risk_register.context_snapshot prepend feature. Net behavior: same
   ;; CONTEXT_SNAPSHOT + RECENT_HISTORY shape the fork produced, now without
   ;; the per-iteration full-file RAM cost.
   (let* (($tail (read_file_tail (library OmegaClaw-Core ./memory/history.metta) (maxHistory)))
          ($snapshot (py-call (risk_register.context_snapshot))))
         (py-str ("CONTEXT_SNAPSHOT: " $snapshot " RECENT_HISTORY: " $tail))))

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
