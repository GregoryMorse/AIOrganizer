# ADR-009: Persistent Qt navigation and model/view review

Status: accepted

Use a persistent left navigation rail and one retained widget instance per page so
selection, filtering, scroll position, guidance, and proposal revision remain
stable while switching work. Large reviews use QAbstractItemModel tables. Folder
planning uses one aligned union-diff model, and PDF evidence uses Qt PDF/QPdfView.
