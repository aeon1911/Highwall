"""Utilitários de linha de comando.

  python -m app.cli check-telegram           testa token e acesso ao canal
  python -m app.cli set-role USUARIO admin   muda o papel (user | moderator | admin)
  python -m app.cli recount                  recalcula contadores de tags/personagens
"""

import asyncio
import sys

from sqlalchemy import func, select

from .db import SessionLocal, init_db
from .models import ROLES, Character, User
from .services import refresh_character_counts, refresh_tag_counts


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, *args = argv
    init_db()
    if cmd == "check-telegram":
        from .config import get_settings
        from .storage.telegram import TelegramStorage

        s = get_settings()
        st = TelegramStorage(s.telegram_bot_token, s.telegram_chat_id, s.telegram_api_base)
        info = asyncio.run(st.check())
        print(f"OK — bot @{info['bot']} tem acesso ao chat '{info['chat']}'")
        return 0
    with SessionLocal() as db:
        if cmd == "set-role" and len(args) == 2 and args[1] in ROLES:
            u = db.scalar(select(User).where(func.lower(User.username) == args[0].lower()))
            if not u:
                print("Usuário não encontrado")
                return 1
            u.role = args[1]
            db.commit()
            print(f"{u.username} agora é {u.role}")
            return 0
        if cmd == "recount":
            refresh_tag_counts(db)
            refresh_character_counts(db, list(db.scalars(select(Character.id))))
            db.commit()
            print("Contadores atualizados")
            return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
