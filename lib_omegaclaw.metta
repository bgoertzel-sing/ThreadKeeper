!(import! &self (library lib_patrick))
!(import! &self (library lib_llm))
!(import! &self (library lib_vector))
!(import! &self (library lib_combinatorics))
!(import! &self (library OmegaClaw-Core lib_nal))
!(import! &self (library OmegaClaw-Core lib_pln))
;; Deontic/directive stack is intentionally not imported by default in the
;; live Telegram agent. Importing it eagerly makes PeTTa specialize the native
;; deontic grounder during startup (`gb-pivot`, `gb-base`, `gb-scan-item`, ...),
;; which can pin SWI at ~100% CPU before Telegram polling becomes responsive.
;; Keep these libraries opt-in for tests/CLI until the native specialization
;; path is bounded or the Prolog-backed deontic API can be loaded lazily.
!(import! &self (library OmegaClaw-Core lib_llm_ext.py))
!(import! &self (library OmegaClaw-Core ./src/utils))
!(import! &self (library OmegaClaw-Core ./src/helper.py))
!(import! &self (library OmegaClaw-Core ./src/agentverse.py))
!(import! &self (library OmegaClaw-Core ./channels/irc.py))
!(import! &self (library OmegaClaw-Core ./channels/mattermost.py))
!(import! &self (library OmegaClaw-Core ./channels/telegram.py))
!(import! &self (library OmegaClaw-Core ./channels/slack.py))
!(import! &self (library OmegaClaw-Core ./channels/websearch.py))
!(import! &self (library OmegaClaw-Core ./src/channels))
!(import! &self (library OmegaClaw-Core ./src/skills))
!(import! &self (library OmegaClaw-Core ./src/memory))
!(import! &self (library OmegaClaw-Core ./src/context))
!(import! &self (library OmegaClaw-Core ./profile/policy))
!(import! &self (library OmegaClaw-Core ./src/loop))
!(import! &self (library OmegaClaw-Core lib_extensions))
!(import! &self (library OmegaClaw-Core ./src/rag.py))
!(git-import! "https://github.com/patham9/petta_lib_chromadb.git")
!(import! &self (library petta_lib_chromadb lib_chromadb))
