; lib_extensions.metta — late extension registry for OmegaClaw
;
; Two-phase boot support: keep the core Telegram bot boot path small and report
; optional heavyweight stacks after core initialization.
;
; Current safety note: PeTTa can load the deontic/directive package at top level
; for tests/CLI, but invoking `import!` from inside the live loop can still pin
; SWI during native grounder specialization (`gb-pivot`, `gb-base`, ...).  The
; live loader therefore marks these extensions as deferred rather than importing
; them in-process.  This preserves bot responsiveness while keeping an explicit
; configuration/status hook for a future bounded/lazy loader.
;
; Configure with e.g.:
;   OMEGACLAW_LATE_EXTENSIONS=deontic,directive,skills_deontic,policy_guard,nal_bridge
;
; Leave OMEGACLAW_LATE_EXTENSIONS empty to disable all late extension work.

!(import! &self (library OmegaClaw-Core ./src/extensions.py))

(= (load-extensions)
   (progn
     (println! "Loading late extensions...")
     (load-extension-if-configured "deontic")
     (load-extension-if-configured "directive")
     (load-extension-if-configured "skills_deontic")
     (load-extension-if-configured "policy_guard")
     (load-extension-if-configured "nal_bridge")
     (println! "Late extensions phase complete.")))

(= (load-extension-if-configured $name)
   (if (== (py-call (extensions.is_configured $name)) true)
       (defer-heavy-extension $name)
       ()))

(= (defer-heavy-extension $name)
   (progn
     (println! "  deferring heavyweight extension import in live loop")
     (py-call (extensions.mark_deferred $name "in-process MeTTa import can pin SWI; use top-level tests/CLI until bounded lazy loader exists"))))

(= (extension-status)
   (py-call (extensions.status_report)))
