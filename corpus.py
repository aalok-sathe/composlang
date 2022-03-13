
# stdlib modules
from pathlib import Path
from collections import defaultdict
import typing
import fileinput
import pydoc
import itertools
import random
from sys import stderr
import typing
from dataclasses import dataclass
from functools import lru_cache

# installed geenric modules
from more_itertools import peekable
from tqdm import tqdm
from matplotlib import pyplot as plt
import numpy as np

# installed specialized modules
import stanza
from stanza.models.common.doc import Document

# local modules
from wordmatrix import WordGraph


def log(*things, **more_things):
    '''
    placeholder logging method to be changed later
    '''
    print('** info:', *things, **more_things,
          file=stderr
         )

    
@dataclass
class Word:
    text: str = None
    upos: str = None
    # head: Word = None
    
    def __hash__(self):
        return hash((self.text, self.upos))
    def __str__(self):
        return self.text
    def __repr__(self):
        return f'{self.text} ({self.upos})'


class Corpus:
    '''
    A class to read and process data from a corpus in one place;
    accumulate aggregate statistics, etc.
    Expects a dependency-parsed corpus with one token per line.
    Flexible formats are supported as long as they are properly specified.
    The only restriction is that a `sentence_id` column is required in order
    to detect sentence boundaries.
    '''
    
    _fmt = None
    _parsed = True
    _files = None
    _upos_token_stats = None # UPOS -> token -> num_occurrences
    _pair_stats = None # ((token1, upos1), (token2, upos2)) -> num_occerrences 
                        # (in a child-parent relation in a dependency parse)
    _sentences_seen = 0
    _parsed_sentences = None
    
    
    def __init__(self, 
                 directory_or_filelist: typing.Union[Path, str, 
                                                     typing.Iterable[typing.Union[Path, str]]], 
                 n_sentences: int = None, store: bool = False,
                 fmt: typing.List[str] = ('sentence_id:int', 'text:str', 'lemma:str', 
                                          'id:int', 'head:int', 'upos:str', 'deprel:str'),
                 sep: str = '\t', lowercase: bool = True, parsed: bool = True):
        '''
        '''
        for fmt_item in fmt:
            if 'sentence_id:' in fmt_item: break
        else:
            raise ValueError(f'format must include a column similar to "sentence_id:int". '
                             f'Received format "{fmt}" does not contain a sentence_id field.')
        self._fmt = fmt
        self._sep = sep
        self._lower = lowercase
        self._n_sentences = n_sentences or float('inf')
        self._store = store # whether to store read sentences in memory 
                            # (if False, read() does file I/O each time)
        self._parsed_sentences = []
        if parsed: self._parsed = parsed
        else: raise ValueError('must provide depparsed input')
        
        
        def _pathify(fpth: typing.Union[Path, str, typing.Any]):
            '''returns a resolved `Path` object'''
            return Path(fpth).expanduser().resolve()
        
        # either it must be a list of files, or a directory containing files
        try: # assume it is a directory or individual file
            directory_or_filelist = _pathify(directory_or_filelist)
            if directory_or_filelist.is_dir():
                self._files = [*directory_or_filelist.iterdir()]
            else:
                self._files = [directory_or_filelist]
        except TypeError as e:
            # assume we are given a list of filepaths as strs or or Path-like objects
            self._files = list(map(_pathify, directory_or_filelist))
        
        random.seed(42)
        random.shuffle(self._files)
    
        
    def read(self, return_sentences=True) -> Document:
        '''
        Reads a parsed corpus file, up to n_sentences in total
        the corpus file is formatted similar to depparse output produced by 
        Stanza by default, or according to a custom format which must
        be specified at instantiation.
            
        # Example stanza dep-parsed COCA sentence segment that we want to handle
        #   each token is on a separate line
        #   first column indicates sentence_id
        ################################################################
        #       2	A	a	1	2	DET	det
        #       2	mother	mother	2	0	NOUN	root
        #       2	and	and	3	4	CCONJ	cc
        #       2	son	son	4	2	NOUN	conj
        #       2	and	and	5	7	CCONJ	cc
        #       2	a	a	6	7	DET	det
        #       2	trip	trip	7	2	NOUN	conj
        #       2	to	to	8	10	ADP	case
        #       2	a	a	9	10	DET	det
        #       2	hotel	hotel	10	7	NOUN	nmod
        #       2	in	in	11	13	ADP	case
        #       2	the	the	12	13	DET	det
        #       2	mountains	mountain	13	10	NOUN	nmod
        #       2	,	,	14	20	PUNCT	punct
        #       ...
        ################################################################

        Args:
            n_sentences: upper limit of # of sentences to process (default: inf)

        Returns:
            stanza.models.common.doc.Document: a Stanza Document of the entire 
                sentence with the parsed data read in.
        '''   
        if self._store and len(self._parsed_sentences) > 0:
            yield from self._parsed_sentences
            return
        
        # recompute stats each time read() is called if we aren't storing the sentences in memory
        self._upos_token_stats = defaultdict(lambda: defaultdict(int)) # UPOS -> token -> num_occurrences
        self._pair_stats = defaultdict(int)
        self._parsed_sentences = []
        self._sentences_seen = 0
        n_sentences = self._n_sentences
        
        # sum the total lines in a file for tqdm without loading them all in memory
        if n_sentences == float('inf'):
            with fileinput.input(files=self._files) as f:
                total = sum(1 for _ in f)
        else:
            total = n_sentences
        with fileinput.input(files=self._files) as f, tqdm(total=total, leave=False) as T:
            # more_itertools.peekable allows seeing future context without using up item from iterator
            f = peekable(f)

            this_sentence = []
            current_file = fileinput.filename()
            for line in f:
                if total >= float('inf'): T.update(1)
                if line.strip() == '': continue
                
                if current_file != (current_file := fileinput.filename()):
                    log(f'processing {current_file}')

                parse = Corpus.segment_line(line, 
                                            sep=self._sep, fmt=self._fmt)
                # sentence_id is only unique within a filename, so
                # two files containing sequential sentence IDs (e.g., [1,], [1,2,3,])
                # will cause result in the concatenation of two distinct sentences
                # (which would be an issue since the token_ids are valid within a sentence)
                parse['sentence_id'] = f"{fileinput.filename()}_{parse['sentence_id']}"
                this_sentence += [parse]

                # have we crossed a sentence boundary? alternatively, are we out of lines to process?
                dummy_line = f'{parse["sentence_id"] + "_EOF"}{self._sep}' + self._sep.join(map(str, range(6 + 10)))
                next_parse = self.segment_line(f.peek(dummy_line), sep=self._sep, fmt=self._fmt) # uses dummy line if no more lines to process
                next_parse['sentence_id'] = f"{fileinput.filename()}_{next_parse['sentence_id']}"

                if next_parse['sentence_id'] != parse['sentence_id']:
                    # process current sentence and reset the "current sentence"
                    [sent] = Document([this_sentence]).sentences
                    
                    # accumulate statistics about words
                    for w in sent.words:
                        if self._lower: w.text = w.text.lower()
                        self._upos_token_stats[w.upos][w.text] += 1
                    
                    this_sentence = []
                    self._sentences_seen += 1
                    if total < float('inf'): T.update(1)
                    
                    if self._store: 
                        self._parsed_sentences.append(sent)
                    if return_sentences:
                        yield sent

                if self._sentences_seen >= n_sentences:
                    break

            log(f'finished processing after seeing {self._sentences_seen} sentences.')


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
            # otherwise keep it as-is (str)
            typecast = pydoc.locate(typ or 'str')
            try:
                value = typecast(row[i])
            except IndexError as e:
                log('ERR:', line, line.strip().split(sep))
                raise
            doc[label] = value
        
        return doc

    
    def __len__(self):
        return self._n_sentences
    
    
    @classmethod
    def _extract_edges(cls, sentence: stanza.models.common.doc.Sentence) -> (Word, Word):
        for w in sentence.words:
            # if w is root, it has no head, so skip
            if w.head == 0:
                continue
            p = sentence.words[w.head-1]
            yield Word(w.text, w.upos), Word(p.text, p.upos)
    
        
    def extract_upos_pair_counts(self, threshold: int = 2):
        '''
        '''
        if len(self._pair_stats) == 0:
            list(self.extract_edges(threshold=2))
        return self._pair_stats
    
    
    def extract_edges(self,
                      sentences: typing.Iterable[stanza.models.common.doc.Sentence] = None,
                      update_stats: bool = True,
                      threshold: int = 2):
        '''
        extract edges (all head-relations) from either the given sentences,
        or if no sentences are given, read sentences from the Corpus.
        
        Args:
            sentences [typing.Iterable]: an iterable over Stanza Sentence objects 
                to process (optionall). if None is provided, sentences will be read
                either from the stored attribute _parsed_sentences, if non-empty,
                or from the Corpus directly. 
                all of this done to avoid using too much memory; however, if the
                corpus is small and the tradeoff between memory and I/O speed favors
                speed, then the Corpus instance should be created with `store=True` 
            update_stats [bool]: whether the stateful pair-occurrence stats should
                be updated for this instance of Corpus
        '''
        if len(self._pair_stats) > 0:
            for w, p in self._pair_stats:
                for i in range(self._pair_stats[w, p]):
                    yield w, p
        else:
            for sentence in self.read():
                for w, p in Corpus._extract_edges(sentence):
                    
                    # don't store stats for very low frequency words
                    if w not in self.prune_tokens(threshold=threshold): continue
                    if p not in self.prune_tokens(threshold=threshold): continue
                    
                    if update_stats:
                        self._pair_stats[w,p] += 1
                        
                    yield w, p
    
    
    def generate_graph(self, upos: typing.Iterable = (),
                       threshold: int = 2) -> 'nx.Graph':
        '''
        '''
        uposes = set(self._upos_token_stats)
        if upos: 
            uposes.intersection_update(set(upos))
        
        wg = WordGraph(((w,p) for w,p in self.extract_edges(threshold=threshold) 
                        if w.upos in uposes and p.upos in uposes))
        return wg
    
    
    @classmethod
    def _extract_upos_pairs(cls,
                            sentence: stanza.models.common.doc.Sentence, 
                            child_upos, parent_upos, include_context=False) -> tuple:
        # check, for each word, if it is the correct child_upos we are interested in
        # * possible extension to this would be to check by constituent rather than
        #   individual UPOSes
        for w in sentence.words:
            if w.upos == child_upos:
                # get the parent of this word and find out its UPOS
                # token with ID 0 is the root and not present in the token list,
                # so we subtract 1 to get the correct offset
                p = sentence.words[w.head-1]
                if p.upos == parent_upos:
                    # whether the context this pair appeared in should be returned
                    # by default this is false so as not to explode memory usage
                    if include_context:
                        yield Word(w.text,w.upos), Word(p.text,p.upos), sentence.words
                    else:
                        yield Word(w.text,w.upos), Word(p.text,p.upos)

                        
    def extract_upos_pairs(self,
                           child_upos, 
                           parent_upos,
                           include_context=False):
        '''
        Extract pairs of certain UPOSes from the sentences
        '''
        for sentence in self.read():
            yield from Corpus._extract_upos_pairs(sentence,
                                                  child_upos=child_upos, 
                                                  parent_upos=parent_upos, 
                                                  include_context=include_context)
    
    
    def upos_counts(self, unique: bool = False):
        '''
        get counts of the number of occurrences of each upos.
        optionally, if unique=True, consider each token as one occurrence
        '''
        if unique:
            return {upos: len(self._upos_token_stats[upos].values()) 
                    for upos in self._upos_token_stats}
        return {upos: sum(self._upos_token_stats[upos].values()) 
                for upos in self._upos_token_stats}
    

    def token_counts(self, upos: typing.Iterable = ()):
        '''
        get the total number of occurrences of each token,
        restricted to the given uposes (optional), or all uposes available
        '''
        uposes = set(self._upos_token_stats)
        if upos: 
            uposes.intersection_update(set(upos))
        
        return {token: sum(self._upos_token_stats[upos][token] 
                           if token in self._upos_token_stats[upos] 
                           else 0 
                           for upos in uposes)
                for token in (t for u in uposes 
                                for t in self._upos_token_stats[u])}
        
    
    @lru_cache(maxsize=1)
    def prune_tokens(self, threshold=2):
        '''
        '''
        # get token counts across all UPOS
        token_counts = self.token_counts(upos=())
        # filter tokens that occur at least `threshold` number of times
        tokens_to_keep = {t for t in token_counts if token_counts[t] >= threshold}
        return tokens_to_keep

    
    def extract_combinations(self,
                             child_upos, parent_upos,
                             threshold: int = 2,
                             ) -> typing.Iterable[np.array]:        
        
        child_to_parent = defaultdict(int)
        parent_to_child = defaultdict(int)
        
        for (w,p) in self.extract_upos_pair_counts(threshold=threshold):
            if (child_upos == '*' or w.upos == child_upos) and (parent_upos == '*' or p.upos == parent_upos):
                child_to_parent[w] += 1
                parent_to_child[p] += 1
                                                   
        child_to_parent_arr = np.array(sorted(child_to_parent.values(), key=lambda c:-c))
        parent_to_child_arr = np.array(sorted(parent_to_child.values(), key=lambda c:-c))
        
        child_to_parent_labels = np.array(sorted(child_to_parent.keys(), key=lambda k:-child_to_parent[k]))
        parent_to_child_labels = np.array(sorted(parent_to_child.keys(), key=lambda k:-parent_to_child[k]))
        
#         # filter by minimum no. of occurrences
#         child_mask = child_to_parent_arr >= threshold
#         parent_mask = parent_to_child_arr >= threshold
        
#         child_to_parent_arr = child_to_parent_arr[child_mask]
#         child_to_parent_labels = child_to_parent_labels[child_mask]
        
#         parent_to_child_arr = parent_to_child_arr[parent_mask]
#         parent_to_child_labels = parent_to_child_labels[parent_mask]
        
        return child_to_parent_arr, child_to_parent_labels, parent_to_child_arr, parent_to_child_labels
    
    
    
def fit_zipf(freq):
    
    from scipy.stats import linregress
    
    logfreq = np.log(np.sort(freq)[::-1])
    logrank = np.log(np.arange(1, len(freq)+1, 1))
    
    res = linregress(logrank, logfreq)
    return np.exp(res.intercept + res.slope*logrank), res
    
    
    
def plot_rank_freq_distribution(ranked_freq, labels, upos):

    fig, (left, right) = plt.subplots(1,2, figsize=(20,5))

    def plot_comparisons(candidate, axes):
        
        fitted, res = fit_zipf(candidate)
        axes.plot(fitted, '-', label=f'fitted using linregress\n$r={res.rvalue:.2f},p={res.pvalue:.2f}$')
        # axes.fill_between(np.arange(1, len(fitted)+1),
        #                   np.exp(np.log(fitted)-res.stderr), np.exp(np.log(fitted)+res.stderr),
        #                   color='gray', alpha=0.2)
        np.random.seed(1)
        #### ZIPFs for comparison
        for a in np.arange(1.5, 3, .3):
            z = np.sort(np.random.zipf(a, (len(candidate,))))[::-1]
            # z = z[z <= max(candidate)+10]
            axes.plot(z, '--', label=f'draw from Zipf (a={a:.2f})')
        # #### EXP for comparison
        # alpha = 10+np.median(candidate)/np.log(2)
        # exp = np.sort(1+np.random.exponential(alpha, (len(candidate),)))[::-1]
        # axes.plot(exp, '--', label=f'Exp({alpha:.2f})')


    ####################
    #### LEFT
    ####################
    left.plot(ranked_freq, 'b.' if upos in ('NOUN', 'VERB') else 'r.', label=f'{upos} compos.', 
                 linewidth=5, alpha=.7, )

    plot_comparisons(ranked_freq, left)

    left.set(xlabel=f'{upos} compositionality rank',
                ylabel=f'# lexical that combine with {upos}',
                yscale='log')
    left.set_ylim([.9, max(ranked_freq)+10])
    
    ulim = np.ceil(np.log(ranked_freq[0])/np.log(2))+1
    ulim = int(ulim)
    left.set_yticks(2**np.arange(ulim), 2**np.arange(ulim))

    xticks = [*np.arange(1, len(ranked_freq), len(ranked_freq)//10)]
    # log(xticks)
    left.set_xticks(xticks,
                    labels=[f'{a}\n{b:.0e}' for a,b in zip(labels[xticks], xticks)],
                    rotation=60)
    left.legend()


    ####################
    #### RIGHT
    ####################
    right.plot(ranked_freq, 'b.' if upos in ('NOUN', 'VERB') else 'r.',  label=f'{upos} compos.', 
                 linewidth=5, alpha=.7,  )

    plot_comparisons(ranked_freq, right)

    right.set(xlabel=f'{upos} compositionality rank\n(log scale)',
                ylabel=f'# lexical items that combine with {upos}\n(log scale)',
                xscale='log', yscale='log')
    right.set_ylim([.9, max(ranked_freq)+10])

    ulim = np.ceil(np.log(ranked_freq[0])/np.log(2))+1
    ulim = int(ulim)
    right.set_yticks(2**np.arange(ulim), 2**np.arange(ulim))
    
    xticks = [*2**np.arange(ulim)] + [*np.arange(2**ulim, len(ranked_freq), (len(ranked_freq)-2**ulim)//3)]
    # log(xticks)
    right.set_xticks(xticks,
                     labels=[f'{a}\n{b:.0e}' for a,b in zip(labels[xticks], xticks)],
                     rotation=60)
    right.legend()

    return fig, (left, right)