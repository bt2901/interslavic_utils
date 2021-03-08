from __future__ import unicode_literals
import re
import sys
import gzip
import os.path
import bz2file as bz2
import codecs
import logging
import ujson

import xml.etree.cElementTree as ET

from unicodecsv import DictReader
import unicodedata
from string import whitespace

# To add stats collection in inobstrusive way (that can be simply disabled)
from blinker import signal

doubleform_signal = signal('doubleform-found')

def getArr(details_string):
    return [x for x in details_string
            .replace("./", '/')
            .replace(" ", '')
            .split('.')
            if x != ''
           ]

diacr_letters = "žčšěйćżęųœ" 
plain_letters = "жчшєjчжеуо"


lat_alphabet = "abcčdeěfghijjklmnoprsštuvyzž"
cyr_alphabet = "абцчдеєфгхийьклмнопрсштувызж"


save_diacrits = str.maketrans(diacr_letters, plain_letters)
cyr2lat_trans = str.maketrans(cyr_alphabet, lat_alphabet)
lat2cyr_trans = str.maketrans(lat_alphabet, cyr_alphabet)

def lat2cyr(thestring):

    # "e^" -> "ê"
    # 'z\u030C\u030C\u030C' -> 'ž\u030C\u030C'
    thestring = unicodedata.normalize(
        'NFKC', 
        thestring
    ).lower().replace("\n", " ")

    # remove all diacritics beside haceks/carons
    thestring = unicodedata.normalize(
        'NFKD',
        thestring.translate(save_diacrits)
    )
    filtered = "".join(c for c in thestring if c in whitespace or c.isalpha())
    # cyrillic to latin
    filtered = filtered.replace(
        "đ", "dž").replace(
        # Serbian and Macedonian
        "љ", "ль").replace("њ", "нь").replace(
        # Russian
        "я", "йа").replace("ю", "йу").replace("ё", "йо")
        
    return filtered.translate(lat2cyr_trans).replace("й", "ј").replace("ь", "ј").strip()


def infer_pos(arr):
    if 'adj' in arr:
        return 'adjective'
    if set(arr) & {'f', 'n', 'm', 'm/f'}:
        return 'noun'
    if 'adv' in arr:
        return 'adverb'
    if 'conj' in arr:
        return 'conjunction'
    if 'prep' in arr:
        return 'preposition'
    if 'pron' in arr:
        return 'pronoun';
    if 'num' in arr:
        return 'numeral';
    if 'intj' in arr:
        return 'interjection';
    if 'v' in arr:
        return 'verb';


def export_grammemes_description_to_xml(tag_set):
    grammemes = ET.Element("grammemes")
    for tag in tag_set.full.values():
        grammeme = ET.SubElement(grammemes, "grammeme")
        if tag["parent"] != "aux":
            grammeme.attrib["parent"] = tag["parent"]
        name = ET.SubElement(grammeme, "name")
        name.text = tag["opencorpora tags"]

        alias = ET.SubElement(grammeme, "alias")
        alias.text = tag["name"]

        description = ET.SubElement(grammeme, "description")
        description.text = tag["description"]

    return grammemes


class TagSet(object):
    """
    Class that represents LanguageTool tagset
    Can export it to OpenCorpora XML
    Provides some shorthands to simplify checks/conversions
    """
    def __init__(self, fname):
        self.all = []
        self.full = {}
        self.groups = []
        self.lt2opencorpora = {}

        with open(fname, 'rb') as fp:
            r = DictReader(fp,delimiter=';')

            for tag in r:
                # lemma form column represents set of tags that wordform should
                # have to be threatened as lemma.
                tag["lemma form"] = filter(None, [s.strip() for s in
                                           tag["lemma form"].split(",")])

                tag["divide by"] = filter(
                    None, [s.strip() for s in tag["divide by"].split(",")])

                # opencopropra tags column maps LT tags to OpenCorpora tags
                # when possible
                tag["opencorpora tags"] = (
                    tag["opencorpora tags"] or tag["name"])

                # Helper mapping
                self.lt2opencorpora[tag["name"]] = tag["opencorpora tags"]

                # Parent column links tag to it's group tag.
                # For example parent tag for noun is POST tag
                # Parent for m (masculine) is gndr (gender group)
                if not hasattr(self, tag["parent"]):
                    setattr(self, tag["parent"], [])

                attr = getattr(self, tag["parent"])
                attr.append(tag["name"])

                # aux is our auxiliary tag to connect our group tags
                if tag["parent"] != "aux":
                    self.all.append(tag["name"])

                # We are storing order of groups that appears here to later
                # sort tags by their groups during export
                if tag["parent"] not in self.groups:
                    self.groups.append(tag["parent"])

                self.full[tag["name"]] = tag

    def _get_group_no(self, tag_name):
        """
        Takes tag name and returns the number of the group to which tag belongs
        """

        if tag_name in self.full:
            return self.groups.index(self.full[tag_name]["parent"])
        else:
            return len(self.groups)

    def sort_tags(self, tags):
        def inner_cmp(a, b):
            a_group = self._get_group_no(a)
            b_group = self._get_group_no(b)

            if a_group == b_group:
                return cmp(a, b)
            return cmp(a_group, b_group)

        return sorted(tags, cmp=inner_cmp)


class WordForm(object):
    """
    Class that represents single word form.
    Initialized out of form and tags strings from LT dictionary.
    """
    def __init__(self, form, tags, is_lemma=False):
        if ":&pron" in tags:
            tags = re.sub(
                "([a-z][^:]+)(.*):&pron((:pers|:refl|:pos|:dem|:def|:int" +
                "|:rel|:neg|:ind|:gen)+)(.*)", "pron\\3\\2\\4", tags)
        self.form, self.tags = form, tags

        # self.tags = map(strip_func, self.tags.split(","))
        self.tags = {s.strip() for s in self.tags}
        self.is_lemma = is_lemma

        # tags signature is string made out of sorted list of wordform tags
        # This is a workout for rare cases when some wordform has
        # noun:m:v_naz and another has noun:v_naz:m
        self.tags_signature = ",".join(sorted(self.tags))

        # Here we are trying to determine exact part of speech for this
        # wordform
        self.pos = infer_pos(self.tags)

    def __str__(self):
        return "<%s: %s>" % (self.form, self.tags_signature)

    def __unicode__(self):
        return self.__str__()


class Lemma(object):
    def __init__(self, word, lemma_form_tags):
        self.word = word

        self.lemma_form = WordForm(word, lemma_form_tags, True)
        self.pos = self.lemma_form.pos
        self.forms = {}
        self.common_tags = None

        self.add_form(self.lemma_form)

    def __str__(self):
        return "%s" % self.lemma_form

    @property
    def lemma_signature(self):
        return (self.word,) + tuple(self.common_tags)

    def add_form(self, form):
        # print("lemma_form", self.lemma_form.form, '->', form)
        if self.common_tags is not None:
            self.common_tags = self.common_tags.intersection(form.tags)
        else:
            self.common_tags = set(form.tags)

        if (form.tags_signature in self.forms and
                form.form != self.forms[form.tags_signature][0].form):
            doubleform_signal.send(self, tags_signature=form.tags_signature)

            self.forms[form.tags_signature].append(form)

            logging.debug(
                "lemma %s got %s forms with same tagset %s: %s" %
                (self, len(self.forms[form.tags_signature]),
                 form.tags_signature,
                 ", ".join(map(lambda x: x.form,
                               self.forms[form.tags_signature]))))
        else:
            self.forms[form.tags_signature] = [form]

    def _add_tags_to_element(self, el, tags, mapping):
        # if self.pos in tags:

        # TODO: remove common tags
        # tags = set(tags) - set([self.pos])
        # TODO: translate tags here
        for one_tag in tags:
            ET.SubElement(el, "g", v=mapping.lt2opencorpora.get(one_tag, one_tag))

    def export_to_xml(self, i, mapping, rev=1):
        lemma = ET.Element("lemma", id=str(i), rev=str(rev))
        common_tags = list(self.common_tags or set())

        if not common_tags:
            logging.debug(
                "Lemma %s has no tags at all" % self.lemma_form)

            return None

        output_lemma_form = self.lemma_form.form.lower()
        output_lemma_form = lat2cyr(output_lemma_form)
        l_form = ET.SubElement(lemma, "l", t=output_lemma_form)
        self._add_tags_to_element(l_form, common_tags, mapping)

        for forms in self.forms.values():
            for form in forms:
                output_form = form.form.lower()
                output_form = lat2cyr(output_form)
                el = ET.Element("f", t=output_form)
                if form.is_lemma:
                    lemma.insert(1, el)
                else:
                    lemma.append(el)

                self._add_tags_to_element(el,
                                          set(form.tags) - set(common_tags),
                                          mapping)

        return lemma

def yield_all_simple_adj_forms(forms_obj, pos):
    if "casesSingular" in forms_obj:
        forms_obj['singular'] = forms_obj['casesSingular']
        forms_obj['plural'] = forms_obj['casesPlural']
    for num in ['singular', 'plural']:
        for case, content in forms_obj[num].items():
            for i, animatedness in enumerate(["anim", "inan"]):
                if case == "nom":
                    if num == 'singular':
                        yield content[0], {case, "sing", "masc", animatedness} | pos
                        yield content[1], {case, "sing", "neut", animatedness} | pos
                        yield content[2], {case, "sing", "femn", animatedness} | pos
                    if num == 'plural':
                        masc_form = content[0].split("/")
                        yield masc_form[i], {case, "plur", "masc", animatedness} | pos
                        yield content[1], {case, "plur", "neut", animatedness} | pos
                        yield content[1], {case, "plur", "femn", animatedness} | pos
                elif case == "acc":
                    masc_form = content[0].split("/")
                    if num == 'singular':
                        yield masc_form[i], {case, "sing", "masc", animatedness} | pos
                        yield content[1], {case, "sing", "neut", animatedness} | pos
                        yield content[2], {case, "sing", "femn", animatedness} | pos
                    if num == 'plural':
                        yield masc_form[i], {case, "plur", "masc", animatedness} | pos
                        yield content[1], {case, "plur", "neut", animatedness} | pos
                        yield content[1], {case, "plur", "femn", animatedness} | pos
                else:
                    if num == 'singular':
                        yield content[0], {case, "sing", "masc", animatedness} | pos
                        yield content[0], {case, "sing", "neut", animatedness} | pos
                        yield content[1], {case, "sing", "femn", animatedness} | pos
                    if num == 'plural':
                        yield content[0], {case, "plur", "masc", animatedness} | pos
                        yield content[0], {case, "plur", "neut", animatedness} | pos
                        yield content[0], {case, "plur", "femn", animatedness} | pos

def yield_all_noun_forms(forms_obj, pos, columns):
    for case, data in forms_obj.items():
        for (form, form_name) in zip(data, columns):
            if form is not None:
                yield form, {case, form_name} | pos

VERB_AUX_WORDS = {'(je)', 'sę', '(sųt)', 'ne'}

def yield_all_verb_forms(forms_obj, pos):

    # ====== L-particle ======
    # ['pluperfect', 'perfect', 'conditional']:
    tags = [
        {'m', 'past', 'sing'},
        {'f', 'past', 'sing'},
        {'n', 'past', 'sing'},
        {'past', 'plur'},
    ]
    forms_person = forms_obj['perfect']
    base_forms = forms_person[2:5] + forms_person[7:8]
    for form, meta in zip(base_forms, tags):
        parts = " ".join([p for p in form.split(" ") if p not in VERB_AUX_WORDS])
        yield parts, meta | pos | {'past'}
        
    # ====== Future ======
    # ['future']
    # future uses infinitive and aux verbs
    pass

    # ====== Present and Imperfect ======
    # ['present', 'imperfect']
    tags = [
        {'1per', 'sing'},
        {'2per', 'sing'},
        {'3per', 'sing'},
        {'1per', 'plur'},
        {'2per', 'plur'},
        {'3per', 'plur'},
    ]
    for time in ['present', 'imperfect']:
        for entry, one_tag in zip(forms_obj[time], tags):
            for subentry, add_tag in zip(entry.split(","), [set(), {'alt-form'}]):
                yield subentry, pos | {time} | add_tag | one_tag
    # ====== Imperative ======
    imperatives = forms_obj['imperative'].split(',')
    tags = [
        {'2per'}, {'1per', 'plur'}, {'2per', 'plur'}
    ]
    for subentry, add_tag in zip(imperatives, tags):
        yield subentry, pos | {'impr'} | add_tag

    # ====== Participles ======
    tags = [
        {'m'}, {'f'}, {'n'}
    ]
    for time, meta_tag in zip(
        ['prap', 'prpp', 'pfap', 'pfpp'],
        [{'actv', 'present'}, {'pssv', 'present'}, {'actv', 'past'}, {'pssv', 'past'}]
    ):
        # TODO: will fuck up if multi-word verb
        parts = (forms_obj[time]
            .replace(",", "").replace("(", "") .replace(")", "")
            .split(" "))
        for i, entry in enumerate(parts):
            if i >= 6:
                print(forms_obj)
                raise AssertionError
            current_tag = tags[i % 3] | ({"alt-form"} if i >= 3 else set())
            if i % 3 == 0:
                base_part = entry
                yield entry, pos | meta_tag | current_tag
            else:
                if "-" in entry:
                    full_entry = base_part[:-1] + entry[1:]
                else:
                    full_entry = entry
                yield full_entry, pos | meta_tag | current_tag

    # ====== Infinitive ======
    yield forms_obj['infinitive'], pos | {"INFN"}

    # ====== Gerund ======
    yield forms_obj['gerund'], pos | {"NOUN", "V-be"}


def iterate_json(forms_obj, pos_data, base):
    pos = infer_pos(pos_data)
    if isinstance(forms_obj, str) or pos is None:
        # print(base, pos, pos_data)
        return base, pos_data

    if "adj" in pos:
        yield from  yield_all_simple_adj_forms(forms_obj, pos_data)
        content = forms_obj['comparison']
        yield content['positive'][0], {"positive"} | pos_data
        yield content['comparative'][0], {"comparative"} | pos_data
        # TODO: is it right to treat it as adjective??
        yield content['positive'][1], {"adverb", "positive"} | pos_data
        yield content['comparative'][1], {"adverb", "comparative"} | pos_data
    elif "numeral" in pos or 'pronoun' in pos:
        print('skipping', base, pos)
        return base, pos
        '''
        if base in ["go", "iže", "jego", "on", "ona", "ono", "one", "oni", "jej", "jemu", "jih", "jihny", "jim", "jų", "mu"]:
            print(pos)
            print(base)
            print(forms_obj)
            return base, pos
        # print([[base]])
        if forms_obj['type'] == 'adjective':
            # print("1, adj")
            yield from  yield_all_simple_adj_forms(forms_obj, pos_data)
        else:
            print("1, smth else", forms_obj['type'])
            columns = forms_obj['columns']
            yield from yield_all_noun_forms(forms_obj['cases'], pos_data, columns)
        '''
    elif "verb" in pos:
        if base.startswith("ne "):
            pass
        else:
            for entry, tag in yield_all_verb_forms(forms_obj, pos_data):
                if entry.endswith(" sę"):
                    yield entry[:-3], tag
                else:
                    yield entry, tag
    elif "noun" in pos:
        yield from yield_all_noun_forms(forms_obj, pos_data, ['singular', 'plural'])
    return base, pos_data

    
base_tag_set = {}


class Dictionary(object):
    def __init__(self, fname, mapping):
        if not mapping:
            mapping = os.path.join(os.path.dirname(__file__), "mapping_isv.csv")

        self.mapping = mapping
        self.lemmas = {}

        counter_multiword = 0
        counter_multiword_verb = 0
        counter_se = 0
        with open(fname, "r", encoding="utf8") as fp:
            next(fp)
            for i, line in enumerate(fp):
                raw_data, forms, pos_formatted = line.split("\t")
                word_id, isv_lemma, addition, pos, *rest = ujson.loads(raw_data)
                forms_obj = ujson.loads(forms)
                if " " in isv_lemma and "," not in isv_lemma and isinstance(forms_obj, dict):
                    splitted = isv_lemma.split()
                    if len(splitted) == 2 and "sę" in splitted:
                        counter_se += 1
                    else:
                        counter_multiword += 1
                        if "verb" not in pos_formatted:
                            print(isv_lemma.split(), pos_formatted)
                            print(forms_obj)
                            counter_multiword_verb += 1
                    

                # Here we've found a new lemma, let's add old one to the list
                # and continue

                details_set = set(getArr(pos))
                # if infer_pos is None, then fallback to the first form
                pos = infer_pos(details_set) or pos
                current_lemma = Lemma(
                    isv_lemma,
                    lemma_form_tags=details_set,
                )
                number_forms = set()
                for current_form, tag_set in iterate_json(forms_obj, details_set, isv_lemma):
                    if "/" in current_form:
                        all_forms = current_form.split("/")
                    else:
                        all_forms = [current_form]
                    if len(all_forms) > 2:
                        print(isv_lemma, all_forms)
                        raise NameError
                    for single_form, add_tag in zip(all_forms, [set(), {"alt-form"}]):
                        current_lemma.add_form(WordForm(
                            single_form,
                            tags=tag_set | add_tag,
                        ))
                    if pos in {"noun", "numeral"}:
                        number_forms |= {one_tag for one_tag in tag_set if one_tag in ['singular', 'plural']}
                if len(number_forms) == 1:
                    numeric = {"Sgtm"} if number_forms == {"singular"} else {"Pltm"}
                    current_lemma.lemma_form.tags |= numeric
                # if "adj" in pos:
                    #if isv_lemma == "žučji":
                    #    print(pos, isv_lemma, pos_formatted)
                    #    print(raw_data)
                    #    print(isv_lemma)
                    #    print (current_lemma.lemma_form.tags)
                    #    raise NameError
                self.add_lemma(current_lemma)
        print(counter_multiword)
        print(counter_multiword_verb)
        print(counter_se)

    def add_lemma(self, lemma):
        if lemma is not None:
            self.lemmas[lemma.lemma_signature] = lemma

    def export_to_xml(self, fname):
        tag_set_full = TagSet(self.mapping)
        root = ET.Element("dictionary", version="0.2", revision="1")
        tree = ET.ElementTree(root)
        root.append(export_grammemes_description_to_xml(tag_set_full))
        lemmata = ET.SubElement(root, "lemmata")

        for i, lemma in enumerate(self.lemmas.values()):
            lemma_xml = lemma.export_to_xml(i + 1, tag_set_full)
            if lemma_xml is not None:
                lemmata.append(lemma_xml)

        tree.write(fname, encoding="utf-8")