
import argparse
import fileinput
import pydoc
import time
import typing
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import stanza
# from diskcache import Cache
from joblib import Parallel, delayed
from more_itertools import peekable
from sqlitedict import SqliteDict
from stanza.models.common.doc import Document
from tqdm import tqdm

from src.graph import WordGraph
from src.utils import iterable_from_directory_or_filelist, log
from src.word import \
    Word  # currently this is a lambda x: (x[0], x[1]) for serializability


class Corpus:
    '''
    A class to read and process data from a corpus in one place;
    accumulate aggregate statistics, etc.
    Expects a dependency-parsed corpus with one token per line.
    Flexible formats are supported as long as they are properly specified.
    The only restriction is that a `sentence_id` column is required in order
    to detect sentence boundaries.
    '''
    
    # determining where to obtain input and how to process it
    _fmt, _sep, _lower = None, None, None
    _files = None
    _n_sentences = None

    # variables related to tracking the current state of reading the corpus
    # for pausing and resuming
    _current_file = None
    _lines_read = 0
    _sentences_seen = 0
    _total = None

    # tracking the linguistic features of interest within the corpus
    _token_stats = None # (token:str, upos:str) -> num_occ:int
    _pair_stats = None # ((token1:str, upos1:str), (token2, upos2)) -> num_occ:int 
                       # in a child-parent relation in a dependency parse
    _cache = None


    def __init__(self, 
                 directory_or_filelist: typing.Union[Path, str, 
                                                     typing.Iterable[typing.Union[Path, str]]], 

                 cache_dir: typing.Union[str, Path] = './cache/composlang',
                 cache_tag: str = None,

                 n_sentences: int = None, store: bool = False,
                 fmt: typing.List[str] = ('sentence_id:int', 'text:str', 'lemma:str', 
                                          'id:int', 'head:int', 'upos:str', 'deprel:str'),
                 sep: str = '\t', lowercase: bool = True
                 ):
        ''' '''
        self._fmt = fmt
        self._sep = sep
        self._lower = lowercase
        self._files = sorted(iterable_from_directory_or_filelist(directory_or_filelist))
        if self._files is None:
            raise ValueError(f'could not find files at {directory_or_filelist}')
        
        self._pair_stats = Counter() # (token, token) -> num_occurrences
        self._token_stats = Counter() # token -> num_occurrences
        
        # loads the pre-existing _pair_stats and _token_stats objects, or creates empty ones
        self._cache_dir = cache_dir
        self._cache_tag = cache_tag
        self.cache = self._get_cache()
        self.load_cache()
        self._n_sentences = n_sentences or float('inf') # upper limit for no. of sentences to process
        
 
    def __len__(self) -> int:
        return self._sentences_seen
    
    # use context manager to handle closing of cache
    def __enter__(self):
        return self # nothing to do here
    def __exit__(self, *args, **kws):
        self._close_cache()


    def _close_cache(self):
        try:
            self.cache.commit()
            self.cache.close()
        except Exception as e:
            log(f'encountered error in closing cache: {e}')
            pass

    def _get_cache(self, prefix=None, tag=None):
        '''
        creates and returns an SqliteDict() object
        '''
        tag = tag or self._cache_tag
        if tag is None:
            from hashlib import shake_128
            s = shake_128()
            s.update(bytes(' '.join(map(str, self._files)), 'utf-8'))
            self._cache_tag = tag = s.hexdigest(5)

        root = Path(prefix or self._cache_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        root /= tag
        return SqliteDict(str(root), flag='c')

    _attrs_to_cache = (('_sentences_seen', int), ('_lines_read', int), 
                      ('_current_file', lambda: None), ('_pair_stats', Counter), 
                      ('_token_stats', Counter), ('_files', lambda: None), 
                      ('_total', lambda: None), ('_n_sentences', lambda: None))

    def load_cache(self, allow_empty=True):
        '''
        recover core data of this instance from cache 
        '''
        log(f'attempt loading from cache at {self._cache_dir}/{self._cache_tag}')
        for attr, default in self._attrs_to_cache:
            try:
                obj = self.cache[attr]
                setattr(self, attr, obj)
            except KeyError as e:
                log(f'could not find attribute {attr} in cache')
                if not allow_empty:
                    raise e
        log(f'successfully loaded cached data from {self._cache_dir}/{self._cache_tag}')

    def to_cache(self):
        '''
        dump critical state data of this instance to cache
        '''
        log(f'caching to {self._cache_dir}/{self._cache_tag}')
        for attr, _ in self._attrs_to_cache:
            obj = getattr(self, attr)
            self.cache[attr] = obj

        start = time.process_time()
        self.cache.commit()
        end = time.process_time()
        log(f'successfully cached to {self._cache_dir}/{self._cache_tag} in {end-start:.3f} seconds')
        

    def read(self, batch_size:int=1_024) -> Document:
        '''
        Reads a parsed corpus file 
        the corpus file is formatted similar to depparse output below, 
        or according to a custom format which specified at instantiation.
        '''
        
        dummy_line = lambda: f'{"__EOF__"}{self._sep}' + self._sep.join(map(str, range(6 + 10)))
        # if we are resuming from previous state, we want to skip lines that are already processed.
        lines_to_skip = self._lines_read
        n_sentences = self._n_sentences # upper limit

        # sum the total lines in a file for tqdm without loading them all in memory
        if self._total is None:
            if n_sentences >= float('inf'):
                with fileinput.input(files=self._files) as f:
                    self._total = sum(1 for _ in tqdm(f, desc=f'counting # of lines in corpus'))
                log(f'Preparing to read {self._total} lines')
            else:
                self._total = n_sentences
                log(f'Preparing to read {self._total} sentences')

        anchor_time = time.process_time()
        with fileinput.input(files=self._files) as f, tqdm(total=self._total, leave=False) as T:
            # more_itertools.peekable allows seeing future context without using up item from iterator
            f = peekable(f)

            if n_sentences >= float('inf'): # process all sentences; progressbar tracks # of lines
                T.update(self._lines_read)
            else: # process a predetermiend # of sentences; also reflected in progressbar
                T.update(self._sentences_seen)

            sentence_batch = [] # accumulate parsed sentences to process concurrently, saving time
            this_sentence = []
            self._current_file = fileinput.filename()

            for line in f:
                # skip lines to catch up to the previously stored state
                if lines_to_skip > 0:
                    lines_to_skip -= 1
                    continue
                    
                # if line.strip() == '': continue
                if self._current_file != fileinput.filename():
                    self._current_file = fileinput.filename()
                    log(f'processing {self._current_file}')

                parse = self.segment_line(line, sep=self._sep, fmt=self._fmt)
                # sentence_id is only unique within a filename, so two files containing sequential
                # sentence IDs (e.g., [1,], [1,2,3,]) will cause result in the concatenation of two
                # distinct sentences (which would be an issue since the token_ids are valid within a sentence)
                parse['sentence_id'] = f"{fileinput.filename()}_{parse['sentence_id']}"
                this_sentence += [parse]

                # have we crossed a sentence boundary? alternatively, are we out of lines to process?
                next_parse = self.segment_line(f.peek(dummy_line()), sep=self._sep, fmt=self._fmt) # uses dummy line if no more lines to process
                next_parse['sentence_id'] = f"{fileinput.filename()}_{next_parse['sentence_id']}"
                if next_parse['sentence_id'] != parse['sentence_id']:

                    # process current sentence and reset the "current sentence"
                    [sent] = Document([this_sentence]).sentences
                    sentence_batch += [sent]

                    if (sents_read := len(sentence_batch)) >= batch_size or self._sentences_seen+sents_read >= n_sentences:
                        
                        self.digest_sentencebatch(sentence_batch)
                        lines_read = sum(len(s.words) for s in sentence_batch)
                        sentence_batch = []

                        self._lines_read += lines_read
                        self._sentences_seen += sents_read
                        if self._total < float('inf'): 
                            T.update(sents_read)
                        else: 
                            T.update(lines_read)
                        self.to_cache()

                        this_time = time.process_time()
                        log(f'processed sentence_batch of size {sents_read} in {this_time-anchor_time:.3f} sec '
                            f'({sents_read/(this_time-anchor_time):.3f} sents/sec)')
                        log(f'accumulated unique tokens: {len(self._token_stats):,}; '
                            f'accumulated unique pairs: {len(self._pair_stats):,}; '
                            f'sentences seen: {self._sentences_seen:,}')
                        anchor_time = this_time

                    this_sentence = []
                    

                if self._sentences_seen >= n_sentences:
                    self.to_cache()
                    break

            log(f'finished processing after seeing {self._sentences_seen} sentences.')


    def digest_sentencebatch(self, sb: list):
        ''' '''
        # accumulate statistics about words and word pairs in the sentence
        stats = Parallel(n_jobs=-2)(delayed(self._digest_sentence)(sent) for sent in sb)
        for token_stat, pair_stat in stats:
            self._token_stats.update(token_stat)
            self._pair_stats.update(pair_stat)


    @classmethod
    def _digest_sentence(cls, sent) -> typing.Tuple[Counter, Counter]:
        '''
        "digests" a sentence into counts of tokens and token pairs in it
        '''
        ts = Counter([Word(w.text, w.upos) for w in sent.words])
        ps = Counter(cls._extract_edges_from_sentence(sent))
        return ts, ps


    @classmethod
    def segment_line(cls, line: str, sep:str, fmt:typing.Iterable[str]) -> dict:
        '''
        Reads the columns from a line corresponding to a single token in a parse. 
        
        Returns them as a labeled dictionary, with labels corresponding to the 
            `fmt` list in the order of appearance.
        '''
        doc = {}
        row = line.strip().split(sep)
        # if fmt is specified, override self._fmt; else fall back on self._fmt
        for i, item in enumerate(fmt):
            label, typ = item.split(':')
            # if an explicit Python type is provided, cast the label to it
            typecast = pydoc.locate(typ or 'str')
            try:
                value = typecast(row[i])
            except IndexError as e:
                log('ERR:', line, line.strip().split(sep))
                raise
            doc[label] = value
        
        return doc

    
    @classmethod
    def _extract_edges_from_sentence(cls, 
                                     sentence: stanza.models.common.doc.Sentence) -> \
                                     typing.Iterable[typing.Tuple[Word, Word]]:
        edges = []
        for w in sentence.words:
            # if w is root, it has no head, so skip
            if w.head == 0:
                continue
            p = sentence.words[w.head-1]
            edges += [(Word(w.text, w.upos), Word(p.text, p.upos))]

        return edges 
        
    
    def extract_upos_pairs(self,
                           child_upos, 
                           parent_upos,
                           include_context=False):
        '''
        Extract pairs of certain UPOSes from the sentences
        '''
        for w, p in self._pair_stats:
            if w.upos == child_upos and p.upos == parent_upos:
                yield w, p


    def generate_graph(self, child_upos: str = None, parent_upos: str = None,
                       ) -> 'nx.Graph':
        '''
        '''
        wg = WordGraph(((w,p) for w,p in self.extract_edges() 
                        if (child_upos is None or w.upos == child_upos) and \
                           (parent_upos is None or p.upos == parent_upos)))

        return wg


    # TODO
    def upos_counts(self, unique: bool = False):
        '''
        get counts of the number of occurrences of each upos.
        optionally, if unique=True, consider each token as one occurrence
        '''
        if unique:
            return {upos: len(self._upos_token_stats[upos].values()) 
                    for upos in self._upos_token_stats}
        return Counter(upos for token, upos in self._token_stats)
    

    def token_counts(self, upos: typing.Iterable = ()):
        '''
        get the total number of occurrences of each token,
        restricted to the given uposes (optional), or all uposes available
        '''
        uposes = set(self._upos_token_stats.keys())
        if upos: 
            uposes.intersection_update(set(upos))
        
        token_counts = defaultdict(int)
        for upos in uposes:
            for token in self._upos_token_stats[upos].keys():
                token_counts[token] += self._upos_token_stats[upos][token] 
        
        return token_counts
                           
    
    def extract_combinations(self,
                             child_upos, parent_upos,
                             unique: bool = True,
                             ) -> typing.Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:    

        child_to_parent = defaultdict(int)
        parent_to_child = defaultdict(int)
        
        for (w,p), ct in self._pair_stats.items():
            if (child_upos == '*' or w.upos == child_upos) and (parent_upos == '*' or p.upos == parent_upos):
                child_to_parent[w] += 1 if unique else ct
                parent_to_child[p] += 1 if unique else ct
                                                   
        child_to_parent_arr = np.array(sorted(child_to_parent.values(), key=lambda c:-c))
        parent_to_child_arr = np.array(sorted(parent_to_child.values(), key=lambda c:-c))
        
        child_to_parent_labels = np.array(sorted(child_to_parent.keys(), key=lambda k:-child_to_parent[k]))
        parent_to_child_labels = np.array(sorted(parent_to_child.keys(), key=lambda k:-parent_to_child[k]))
        
        return child_to_parent_arr, child_to_parent_labels, parent_to_child_arr, parent_to_child_labels
    
    
if __name__ == '__main__':
    p = argparse.ArgumentParser('composlang')
    p.add_argument('--path', help='Path to directory storing corpus files, or path to a text file',
                   required=True)

    args = p.parse_args()
    print(args)

    with Corpus(args.path, 
                cache_dir='./cache', cache_tag='coca_inf', 
                n_sentences=float('inf')) as coca:
        coca.read()
