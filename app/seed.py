"""Dados iniciais: categorias (com cores), árvore de categorias de tags e tags básicas."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Category, CategoryColor, Tag, TagAlias, TagCategory
from .utils import slugify

CATEGORIES = [
    ("Fotografia", "Ensaios e fotos de modelos reais.", [("#e8b896", "pele"), ("#000000", "preto"), ("#cc0000", "vermelho"), ("#f5e6d3", "creme")]),
    ("Anime & Mangá", "Ilustrações de personagens de anime e mangá.", [("#ff6699", "rosa"), ("#9966cc", "lavanda"), ("#66ccff", "ciano"), ("#ffffff", "branco")]),
    ("Cosplay", "Fotos de cosplayers caracterizados.", [("#663399", "roxo"), ("#ff3366", "rosa choque"), ("#424153", "grafite")]),
    ("Arte Digital", "Pinturas e ilustrações digitais originais.", [("#330066", "índigo"), ("#ffcc33", "dourado"), ("#00cccc", "turquesa")]),
    ("3D / CGI", "Renders 3D e arte gerada em engines.", [("#003399", "azul"), ("#ff9900", "laranja"), ("#999999", "cinza")]),
    ("Games", "Personagens e cenas de jogos.", [("#990000", "carmim"), ("#339933", "verde"), ("#000066", "marinho")]),
]

# (nome, pai)
TAG_CATEGORIES = [
    ("Geral", None),
    ("Pessoas", None), ("Cabelo", "Pessoas"), ("Corpo", "Pessoas"), ("Modelos", "Pessoas"),
    ("Vestuário", None), ("Lingerie", "Vestuário"), ("Praia & Piscina", "Vestuário"), ("Acessórios", "Vestuário"),
    ("Cenário", None), ("Interior", "Cenário"), ("Exterior", "Cenário"),
    ("Estilo", None), ("Iluminação", "Estilo"), ("Fotografia", "Estilo"),
    ("Conteúdo", None),
]

# (nome, categoria de tag, pureza, aliases, implica)
TAGS = [
    ("lingerie", "Lingerie", "sketchy", ["underwear", "roupa íntima"], []),
    ("lingerie preta", "Lingerie", "sketchy", ["black lingerie"], ["lingerie"]),
    ("lingerie vermelha", "Lingerie", "sketchy", ["red lingerie"], ["lingerie"]),
    ("renda", "Lingerie", "sketchy", ["lace"], []),
    ("meia 7/8", "Lingerie", "sketchy", ["stockings", "meia arrastão"], []),
    ("biquíni", "Praia & Piscina", "sketchy", ["bikini", "biquini"], []),
    ("salto alto", "Acessórios", "sfw", ["high heels", "heels"], []),
    ("ruiva", "Cabelo", "sfw", ["redhead", "ruivo"], []),
    ("loira", "Cabelo", "sfw", ["blonde", "loiro"], []),
    ("morena", "Cabelo", "sfw", ["brunette"], []),
    ("cabelo longo", "Cabelo", "sfw", ["long hair"], []),
    ("tatuagem", "Corpo", "sfw", ["tattoo", "tattoos"], []),
    ("praia", "Exterior", "sfw", ["beach"], []),
    ("piscina", "Exterior", "sfw", ["pool"], []),
    ("quarto", "Interior", "sfw", ["bedroom", "cama"], []),
    ("neon", "Iluminação", "sfw", ["neon lights"], []),
    ("luz natural", "Iluminação", "sfw", ["natural light"], []),
    ("preto e branco", "Fotografia", "sfw", ["monochrome", "p&b"], []),
    ("boudoir", "Fotografia", "sketchy", [], []),
    ("cosplay", "Geral", "sfw", [], []),
    ("topless", "Conteúdo", "nsfw", [], []),
    ("nude", "Conteúdo", "nsfw", ["nua", "nu", "nudez"], []),
    ("nude artístico", "Conteúdo", "nsfw", ["artistic nude"], ["nude"]),
]


def seed_defaults(db: Session) -> None:
    if db.scalar(select(Category.id).limit(1)) is None:
        for i, (name, desc, colors) in enumerate(CATEGORIES):
            db.add(
                Category(
                    name=name,
                    slug=slugify(name),
                    description=desc,
                    sort_order=i,
                    colors=[CategoryColor(hex=h, label=lbl, position=j) for j, (h, lbl) in enumerate(colors)],
                )
            )

    if db.scalar(select(TagCategory.id).limit(1)) is None:
        by_name: dict[str, TagCategory] = {}
        for name, parent in TAG_CATEGORIES:
            tc = TagCategory(name=name, slug=slugify(f"{parent}-{name}" if parent else name), parent=by_name.get(parent))
            by_name[name] = tc
            db.add(tc)
        db.flush()

        if db.scalar(select(Tag.id).limit(1)) is None:
            tags: dict[str, Tag] = {}
            for name, cat, purity, aliases, _ in TAGS:
                t = Tag(name=name, slug=slugify(name), purity=purity, tag_category=by_name[cat])
                t.aliases = [TagAlias(alias=a) for a in aliases]
                tags[name] = t
                db.add(t)
            db.flush()
            for name, *_, implies in TAGS:
                for imp in implies:
                    tags[name].implies.append(tags[imp])
    db.commit()
