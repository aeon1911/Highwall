from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

PURITIES = ("sfw", "sketchy", "nsfw")
STATUSES = ("pending", "approved", "rejected")
ROLES = ("user", "moderator", "admin")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- usuários


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    birthdate: Mapped[date] = mapped_column(Date)
    role: Mapped[str] = mapped_column(String(16), default="user")
    bio: Mapped[str] = mapped_column(Text, default="")
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    # filtros de pureza padrão do usuário, ex.: "sfw,sketchy,nsfw"
    purity_pref: Mapped[str] = mapped_column(String(32), default="sfw,sketchy,nsfw")
    # Derivação no navegador (modelo Bitwarden). Com kdf_salt preenchido, `password_hash` é
    # argon2(auth_hash) e o servidor nunca vê a senha real. Sem ele = conta antiga (senha crua).
    kdf_salt: Mapped[str | None] = mapped_column(String(64))
    kdf_iterations: Mapped[int | None] = mapped_column(Integer)
    recovery_auth_hash: Mapped[str | None] = mapped_column(String(255))  # argon2 do autenticador das 12 palavras
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    wallpapers: Mapped[list[Wallpaper]] = relationship(back_populates="uploader", foreign_keys="Wallpaper.uploader_id")

    @property
    def client_kdf(self) -> bool:
        return self.kdf_salt is not None

    @property
    def is_staff(self) -> bool:
        return self.role in ("moderator", "admin")


# --------------------------------------------------------------------------- categorias


class Category(Base):
    """Categoria principal (uma por wallpaper), com paleta de cores associada."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    colors: Mapped[list[CategoryColor]] = relationship(
        back_populates="category", cascade="all, delete-orphan", order_by="CategoryColor.position"
    )


class CategoryColor(Base):
    """Cor definida pela moderação como identidade/tema da categoria."""

    __tablename__ = "category_colors"

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    hex: Mapped[str] = mapped_column(String(7))
    label: Mapped[str] = mapped_column(String(48), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)

    category: Mapped[Category] = relationship(back_populates="colors")


# --------------------------------------------------------------------------- tags


class TagCategory(Base):
    """Grupo hierárquico de tags (ex.: Pessoas > Modelos, Estilo > Lingerie)."""

    __tablename__ = "tag_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("tag_categories.id", ondelete="SET NULL"))

    parent: Mapped[TagCategory | None] = relationship(remote_side="TagCategory.id", back_populates="children")
    children: Mapped[list[TagCategory]] = relationship(back_populates="parent")
    tags: Mapped[list[Tag]] = relationship(back_populates="tag_category")

    @property
    def path(self) -> str:
        parts, node = [], self
        while node is not None:
            parts.append(node.name)
            node = node.parent
        return " › ".join(reversed(parts))


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    purity: Mapped[str] = mapped_column(String(8), default="sfw")
    tag_category_id: Mapped[int | None] = mapped_column(ForeignKey("tag_categories.id", ondelete="SET NULL"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tag_category: Mapped[TagCategory | None] = relationship(back_populates="tags")
    aliases: Mapped[list[TagAlias]] = relationship(back_populates="tag", cascade="all, delete-orphan")
    implies: Mapped[list[Tag]] = relationship(
        secondary="tag_implications",
        primaryjoin="Tag.id == TagImplication.tag_id",
        secondaryjoin="Tag.id == TagImplication.implied_id",
    )


class TagAlias(Base):
    """Nome alternativo que resolve para uma tag canônica (ex.: 'lingerie preta' → 'black lingerie')."""

    __tablename__ = "tag_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    tag: Mapped[Tag] = relationship(back_populates="aliases")


class TagImplication(Base):
    """Tag A implica tag B: ao aplicar A, B é adicionada automaticamente."""

    __tablename__ = "tag_implications"
    __table_args__ = (CheckConstraint("tag_id <> implied_id"),)

    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)
    implied_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)


# --------------------------------------------------------------------------- personagens


class Series(Base):
    """Franquia / obra de origem dos personagens (anime, jogo, filme...)."""

    __tablename__ = "series"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(96), unique=True)
    slug: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="")  # anime, jogo, filme, original...

    characters: Mapped[list[Character]] = relationship(back_populates="series")


class Character(Base):
    __tablename__ = "characters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(96))
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id", ondelete="SET NULL"))
    aliases: Mapped[str] = mapped_column(Text, default="")  # separados por vírgula
    description: Mapped[str] = mapped_column(Text, default="")
    usage_count: Mapped[int] = mapped_column(Integer, default=0)

    series: Mapped[Series | None] = relationship(back_populates="characters")

    @property
    def alias_list(self) -> list[str]:
        return [a.strip() for a in self.aliases.split(",") if a.strip()]


# --------------------------------------------------------------------------- wallpapers


class Wallpaper(Base):
    __tablename__ = "wallpapers"
    __table_args__ = (
        Index("ix_wall_status_created", "status", "created_at"),
        Index("ix_wall_res", "width", "height"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    uploader_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(140), default="")
    source: Mapped[str] = mapped_column(String(500), default="")
    purity: Mapped[str] = mapped_column(String(8), default="sketchy", index=True)
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)

    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    ratio: Mapped[float] = mapped_column(Float)
    ratio_label: Mapped[str] = mapped_column(String(12))
    file_size: Mapped[int] = mapped_column(Integer)
    mime: Mapped[str] = mapped_column(String(32))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    dhash: Mapped[str] = mapped_column(String(16), index=True)  # hash perceptual p/ quase-duplicatas

    # referências do armazenamento (file_id do Telegram ou caminho local)
    storage_backend: Mapped[str] = mapped_column(String(16))
    file_ref: Mapped[str] = mapped_column(String(255))
    file_message_id: Mapped[int | None] = mapped_column(Integer)
    thumb_ref: Mapped[str] = mapped_column(String(255))
    thumb_message_id: Mapped[int | None] = mapped_column(Integer)

    views: Mapped[int] = mapped_column(Integer, default=0)
    favorites_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    uploader: Mapped[User] = relationship(back_populates="wallpapers", foreign_keys=[uploader_id])
    category: Mapped[Category | None] = relationship()
    tag_links: Mapped[list[WallpaperTag]] = relationship(back_populates="wallpaper", cascade="all, delete-orphan")
    character_links: Mapped[list[WallpaperCharacter]] = relationship(
        back_populates="wallpaper", cascade="all, delete-orphan"
    )
    colors: Mapped[list[WallpaperColor]] = relationship(
        back_populates="wallpaper", cascade="all, delete-orphan", order_by="WallpaperColor.percent.desc()"
    )

    @property
    def tags(self) -> list[Tag]:
        return sorted((link.tag for link in self.tag_links), key=lambda t: t.name)

    @property
    def characters(self) -> list[Character]:
        return [link.character for link in self.character_links]

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def orientation(self) -> str:
        return "landscape" if self.width >= self.height else "portrait"


class WallpaperTag(Base):
    __tablename__ = "wallpaper_tags"

    wallpaper_id: Mapped[int] = mapped_column(ForeignKey("wallpapers.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True)
    added_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    implied: Mapped[bool] = mapped_column(Boolean, default=False)  # veio de uma implicação
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    wallpaper: Mapped[Wallpaper] = relationship(back_populates="tag_links")
    tag: Mapped[Tag] = relationship()


class WallpaperCharacter(Base):
    __tablename__ = "wallpaper_characters"

    wallpaper_id: Mapped[int] = mapped_column(ForeignKey("wallpapers.id", ondelete="CASCADE"), primary_key=True)
    character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), primary_key=True, index=True
    )

    wallpaper: Mapped[Wallpaper] = relationship(back_populates="character_links")
    character: Mapped[Character] = relationship()


class WallpaperColor(Base):
    """Cor dominante extraída da imagem, já mapeada para a paleta fixa do site."""

    __tablename__ = "wallpaper_colors"
    __table_args__ = (Index("ix_wcolor_hex_pct", "hex", "percent"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    wallpaper_id: Mapped[int] = mapped_column(ForeignKey("wallpapers.id", ondelete="CASCADE"), index=True)
    hex: Mapped[str] = mapped_column(String(7))
    percent: Mapped[float] = mapped_column(Float)

    wallpaper: Mapped[Wallpaper] = relationship(back_populates="colors")


# --------------------------------------------------------------------------- interação


class Favorite(Base):
    __tablename__ = "favorites"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    wallpaper_id: Mapped[int] = mapped_column(ForeignKey("wallpapers.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("wallpaper_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    wallpaper_id: Mapped[int] = mapped_column(ForeignKey("wallpapers.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(String(32))
    details: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | resolved | dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    wallpaper: Mapped[Wallpaper] = relationship()
    user: Mapped[User] = relationship()


# --------------------------------------------------------------------------- galeria privada


class Collection(Base):
    """Pasta da galeria privada do usuário (só o dono vê)."""

    __tablename__ = "collections"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    parent: Mapped[Collection | None] = relationship(remote_side="Collection.id", back_populates="children")
    children: Mapped[list[Collection]] = relationship(back_populates="parent", cascade="all, delete-orphan")
    items: Mapped[list[CollectionItem]] = relationship(
        back_populates="collection", cascade="all, delete-orphan", order_by="CollectionItem.created_at.desc()"
    )


class CollectionItem(Base):
    __tablename__ = "collection_items"

    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True)
    wallpaper_id: Mapped[int] = mapped_column(ForeignKey("wallpapers.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    collection: Mapped[Collection] = relationship(back_populates="items")
    wallpaper: Mapped[Wallpaper] = relationship()


# --------------------------------------------------------------------------- vault (E2E)


class VaultKey(Base):
    """Chave-mestra do vault, sempre *embrulhada* (cifrada). Gerada no navegador no cadastro.

    wrapped_key          = AES-GCM(KEK, chave-mestra)       KEK vem da senha de login (no navegador)
    recovery_wrapped_key = AES-GCM(chave das 12 palavras, chave-mestra)
    Sem a senha ou as 12 palavras, nada aqui permite decifrar os arquivos."""

    __tablename__ = "vault_keys"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    wrapped_key: Mapped[str] = mapped_column(String(255))
    recovery_wrapped_key: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VaultFolder(Base):
    """Pasta do vault. O nome é cifrado no navegador; o servidor só vê um ID."""

    __tablename__ = "vault_folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name_enc: Mapped[str] = mapped_column(Text)  # base64(iv || AES-GCM(nome))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VaultItem(Base):
    """Arquivo cifrado no navegador. Nome, tipo e dimensões também vão cifrados em `meta`."""

    __tablename__ = "vault_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("vault_folders.id", ondelete="SET NULL"), index=True)
    meta: Mapped[str] = mapped_column(Text)  # base64(iv || AES-GCM(json))
    size: Mapped[int] = mapped_column(Integer)  # tamanho do blob cifrado
    storage_backend: Mapped[str] = mapped_column(String(16))
    file_ref: Mapped[str] = mapped_column(String(255))
    file_message_id: Mapped[int | None] = mapped_column(Integer)
    thumb_ref: Mapped[str] = mapped_column(String(255))
    thumb_message_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
