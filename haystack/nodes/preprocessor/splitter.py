from typing import List, Optional, Union, Dict, Any, Callable

try:
    from typing import Literal, get_args
except ImportError:
    from typing_extensions import Literal, get_args  # type: ignore

import logging
from functools import partial
from pathlib import Path

import nltk
from tqdm.auto import tqdm
from transformers import PreTrainedTokenizer

from haystack.nodes.base import BaseComponent
from haystack.schema import Document
from haystack.nodes.preprocessor.split_helpers import (
    split_by_regex,
    split_by_sentence_tokenizer,
    split_by_separator,
    split_by_separators,
    split_by_transformers_tokenizer,
    load_sentence_tokenizer,
)
from haystack.nodes.preprocessor.merge_helpers import validate_unit_boundaries, make_merge_groups
from haystack.modeling.model.feature_extraction import FeatureExtractor


logger = logging.getLogger(__name__)


REGEX_METACHARS = r".^$*+?{}[]\|()"


SplitBy = Literal["character", "token", "word", "sentence", "paragraph", "page", "regex"]


class DocumentSplitter(BaseComponent):
    """
    Splits documents into smaller, shorter documents.

    Can split on different units ('character', 'token', 'word', 'sentence', 'paragraph', 'page', or 'regex'),
    at different lengths, and include some overlap across the splits.

    It can also properly assign page numbers and re-assign headlines found in the metadata of the parent document
    to each split document.

    No char is lost in splitting, not even whitespace, and all headlines are preserved, However, text and headlines
    duplication may occur if `split_overlap>0`.
    """

    outgoing_edges = 1

    def __init__(
        self,
        split_by: SplitBy,
        split_length: int,
        split_separators: Optional[List[str]] = None,
        split_regex: Optional[str] = None,
        split_overlap: int = 0,
        max_chars: int = 2000,
        max_tokens: int = 0,
        tokenizer_model: Optional[
            Union[Literal["word"], Path, PreTrainedTokenizer, FeatureExtractor, Callable]
        ] = "word",
        nltk_language: str = "english",
        nltk_folder: Optional[str] = None,
        progress_bar: bool = True,
        add_page_number: bool = True,
    ):
        """
        Splits documents into smaller, shorter documents.

        Can split on different units ('character', 'token', 'word', 'sentence', 'paragraph', 'page', or 'regex'),
        at different lengths, and include some overlap across the splits.

        It can also properly assign page numbers and re-assign headlines found in the metadata of the parent document
        to each split document.

        No char is lost in splitting, not even whitespace, and all headlines are preserved, However, text and headlines
        duplication may occur if `split_overlap>0`.

        :param split_by: Splitting strategy. Can be 'character', 'token', 'word', 'sentence', 'paragraph', 'page', 'regex', 'separators'.

        :param split_separators: If `split_by="separators"`, provide here a list of separators. For example, if the document
                            should be split on "--my separator--" or "$$ another separator $$", this field should be set to
                            `split_separators=["--my separator--", "$$ another separator $$"]`. Generally faster than `regex`.

        :param split_regex: If `split_by="regex"`, provide here a regex matching the separator. For example, if the document
                            should be split on "~~ Chapter <number> ~~", this field should be `split_regex="(~~ Chapter [0-9]* ~~)"`.

        :param split_length: The maximum number of the above split unit (like word, sentence, page and so on) that are allowed in one document.
                                For instance, if `split_length=10` and `split_by="sentence"`, then each output document will contain 10 sentences.\n
                                Note that split_length can be set to 0 to mean "infinite": this option can be used with `max_tokens`.

        :param split_overlap: Units (for example words or sentences) overlap between two adjacent documents after a split.
                                For example, if `split_by="word" and split_length=5 and split_overlap=2`, then the splits would be like:
                                `[w1 w2 w3 w4 w5, w4 w5 w6 w7 w8, w7 w8 w10 w11 w12]`.
                                Set the value to 0 to ensure there is no overlap among the documents after splitting.

        :param max_chars: Absolute maximum number of chars allowed in a single document. Reaching this boundary
                            cuts the document, even mid-word, and logs a loud error. This parameter has higher priority than
                            both `split_length` and `max_tokens`.\n
                            It's recommended to set this value to approximately double the size you expect your documents
                            to be. For example, with `split_by='sentence'`, `split_length=2`, if the average sentence
                            length of our document is 100 chars, you should set `max_char=400` or `max_char=500`.\n
                            This is a safety parameter to avoid extremely long documents to end up in the document store.
                            Keep in mind that huge documents (tens of thousands of chars) will strongly impact the
                            performance of Reader nodes and can drastically slow down the indexing speed.

        :param max_tokens: Maximum number of tokens that are allowed in a single split. This helps you to ensure that
                            your transformer model doesn't get an input sequence longer than it can handle. If set to
                            0, it will be ignored. If set to any value above 0, you also need to give a value to
                            `tokenizer_model`. This is typically the tokenizer of the transformer in your pipeline that
                            has the shortest `max_seq_len` parameter. \n
                            Note that `max_tokens` has a higher priority than `split_length`. This means the number
                            of tokens included in the split documents will never be above the `max_tokens` value:
                            we rather stop before reaching the value of `split_length`.\nFor example:

                            ```python
                            DocumentSplitter(split_by='sentence', split_length=10, max_tokens=512, max_chars=2000)
                            ```

                            means:

                            - Documents will contain whole sentences
                            - Documents will contain at most 10 sentences
                            - Documents might contain less than 10 sentences if the maximum number of tokens is
                                reached earlier.
                            - Documents will never contain more than 2000 chars. Documents with a content length
                                above that value will be split on the 2000th character.

                            Note that the number of tokens might still be above the maximum if a single sentence
                            contains more than 512 tokens. In this case an `ERROR` log is emitted, but the document
                            is generated with whatever amount of tokens the first sentence has.\n
                            If the number of units is irrelevant, `split_length` can be safely set at `0`.

        :param tokenizer_model: If `split_by="token"` or `split_max_tokens>0`, you should provide a tokenizer model to compute the tokens.
                                There are several options, depending on the tradeoff you need between precision and speed:
                                - "word". The text is split with the `split()` function (as done by the old PreProcessor).
                                - A tokenizer model. You can give its identifier on Hugging Face Hub, a local path to load it from, or an instance of
                                `PreTrainedTokenizer`.
                                - A lambda function. In this case, make sure it takes one single input parameter called `text`, like
                                  `tokenizer_model=lambda text: text.split("my token delimiter")`

                                Defaults to "word".

        :param nltk_language: If `split_by="sentence"`, the language used by "nltk.tokenize.sent_tokenize", for example "english", or "french".
                                Mind that some languages have limited support by the tokenizer: for example, it seems incapable to split Chinese text
                                by word, but it can correctly split it by sentence.

        :param nltk_folder: If `split_by="sentence"`, specifies the path to the folder containing the NTLK `PunktSentenceTokenizer` models,
                            if loading a model from a local path. Leave empty otherwise.

        :param progress_bar: Whether to show a progress bar.

        :param add_page_number: Add the number of the page a paragraph occurs in to the Document's meta
                                field `"page"`. Page boundaries are determined by `"\f"' character which is added
                                in between pages by `PDFToTextConverter`, `TikaConverter`, `ParsrConverter` and
                                `AzureConverter`.
        """
        super().__init__()
        self._validate_split_params(
            split_by=split_by,
            split_regex=split_regex,
            split_separators=split_separators,
            split_length=split_length,
            split_overlap=split_overlap,
            max_chars=max_chars,
            max_tokens=max_tokens,
        )

        self.split_by = split_by
        self.split_length = split_length
        self.split_overlap = split_overlap
        self.split_regex = split_regex
        self.split_separators = split_separators
        self.max_chars = max_chars
        self.max_tokens = max_tokens
        self.progress_bar = progress_bar
        self.add_page_number = add_page_number

        self._nltk_language = nltk_language
        self._nltk_folder = Path(nltk_folder) if nltk_folder else None

        self.sentence_tokenizer = None
        if split_by == "sentence":
            self.sentence_tokenizer = load_sentence_tokenizer(
                language=nltk_language, tokenizer_model_folder=self.nltk_folder
            )

        self._tokenizer = None
        if tokenizer_model or max_tokens:
            self.tokenizer = tokenizer_model

    def _validate_split_params(
        self,
        split_by: SplitBy,
        split_regex: Optional[str],
        split_separators: Optional[List[str]],
        split_length: int,
        split_overlap: int,
        max_chars: int,
        max_tokens: Optional[int],
    ):
        """
        Performs some basic validation on the parameters of the splitter.
        """
        if split_by not in get_args(SplitBy):
            raise ValueError(f"split_by must be one of: {', '.join(get_args(SplitBy))}")

        if not isinstance(split_length, int) or split_length < 0:
            raise ValueError("split_length must be an integer >= 0")

        if split_length == 0 and not max_tokens:
            logger.warning(
                "split_length is set to 0 and max_tokens is not set. "
                "This means that the documents will be split in chunks of %s chars. "
                "Is this the behavior you expect? If so, set `split_by='character'` "
                "and `split_length=%s` to remove this warning.",
                max_chars,
                max_chars,
            )

        if split_length:
            if not isinstance(split_overlap, int) or split_overlap < 0:
                raise ValueError("split_overlap must be an integer >= 0")

            if split_overlap >= split_length:
                raise ValueError("split_length must be higher than split_overlap")

        if split_regex and not split_by == "regex":
            logger.warning(
                "You provided a value to 'split_regex', but 'split_by=\"%s\"'. "
                "By default, the document will be split by %s and the regex pattern will be ignored.",
                split_by,
                split_by,
            )

        if split_separators and not split_by == "separator":
            logger.warning(
                "You provided a value to 'split_separators', but 'split_by=\"%s\"'. "
                "By default, the document will be split by %s and the separators will be ignored.",
                split_by,
                split_by,
            )

    @property
    def tokenizer(self):
        return self._tokenizer

    @tokenizer.setter
    def tokenizer(self, tokenizer_model=Union[str, Path, PreTrainedTokenizer, FeatureExtractor, Callable]):
        if not tokenizer_model:
            raise ValueError(
                "Can't set the tokenizer to None. "
                "Provide either the string 'word', a Hugging Face identifier, a path to a local tokenizer, "
                "or an instance of Haystack's FeatureExtractor or Transformers' PreTrainedTokenizer. "
                "You can also provide your own lambda function to tokenize text: in this case "
                "make sure it takes only one input parameter called 'text'."
            )
        if isinstance(tokenizer_model, (PreTrainedTokenizer, FeatureExtractor)):
            self._tokenizer = tokenizer_model

        elif isinstance(tokenizer_model, (str, Path)):
            if tokenizer_model == "word":
                self._tokenizer = lambda text: split_by_separators(text=text, separators=[" ", "\n", "\f"])
            else:
                self._tokenizer = FeatureExtractor(pretrained_model_name_or_path=tokenizer_model)

        else:
            self._tokenizer = tokenizer_model

    @property
    def nltk_language(self):
        return self._nltk_language

    @nltk_language.setter
    def nltk_language(self, nltk_language):
        self.sentence_tokenizer = load_sentence_tokenizer(
            language=nltk_language, tokenizer_model_folder=self.nltk_folder
        )
        self._nltk_language = nltk_language

    @property
    def nltk_folder(self):
        return self._nltk_folder

    @nltk_folder.setter
    def nltk_folder(self, nltk_folder):
        self.sentence_tokenizer = load_sentence_tokenizer(
            language=self.nltk_language, tokenizer_model_folder=nltk_folder
        )
        self._nltk_folder = nltk_folder

    def run(  # type: ignore
        self,
        documents: List[Document],
        split_by: Optional[SplitBy] = None,
        split_regex: Optional[str] = None,
        split_separators: Optional[List[str]] = None,
        split_length: Optional[int] = None,
        split_overlap: Optional[int] = None,
        max_chars: Optional[int] = None,
        max_tokens: Optional[int] = None,
        add_page_number: Optional[bool] = None,
        progress_bar: bool = True,
    ):
        """
        Splits documents into smaller, shorter documents.

        Can split on different units ('character', 'token', 'word', 'sentence', 'paragraph', 'page', or 'regex'),
        at different lengths, and include some overlap across the splits.

        It can also properly assign page numbers and re-assign headlines found in the metadata of the parent document
        to each split document.

        No char is lost in splitting, not even whitespace, and all headlines are preserved, However, text and headlines
        duplication may occur if `split_overlap>0`.

        :param documents: The documents to split.
        :param split_by: Unit for splitting the document. Can be 'character', 'token', 'word', 'sentence', 'paragraph', 'page', 'regex'.
        :param split_regex: If `split_by="regex"`, provide here a regex matching the separator. For example, if the document
                            should be split on "--my separator--", this field should be `split_regex="--my separator--"`.
        :param split_length: The maximum number of the above split unit (like word, sentence, page and so on) that are allowed in one document.
                                For instance, if `split_length=10` and `split_by="sentence"`, then each output document will contain 10 sentences.
        :param split_overlap: Units (for example words or sentences) overlap between two adjacent documents after a split.
                                For example, if `split_by="word" and split_length=5 and split_overlap=2`, then the splits would be like:
                                `[w1 w2 w3 w4 w5, w4 w5 w6 w7 w8, w7 w8 w10 w11 w12]`.
                                Set the value to 0 to ensure there is no overlap among the documents after splitting.
        :param max_chars: Absolute maximum number of chars allowed in a single document. Reaching this boundary
                            cuts the document, even mid-word, and logs a loud error.\n
                            It's recommended to set this value to approximately double the size you expect your documents
                            to be. For example, with `split_by='sentence'`, `split_length=2`, if the average sentence
                            length of our document is 100 chars, you should set `max_char=400` or `max_char=500`.\n
                            This is a safety parameter to avoid extremely long documents to end up in the document store.
                            Keep in mind that huge documents (tens of thousands of chars) will strongly impact the
                            performance of Reader nodes and can drastically slow down the indexing speed.
        :param max_tokens:  Maximum number of tokens that are allowed in a single split. If set to 0, it will be
                            ignored. If set to any value above 0, it requires `tokenizer_model` to be set to the
                            model of your Reader and will verify that, whatever your `split_length` value is set
                            to, the number of tokens included in the split documents will never be above the
                            `max_tokens` value. For example:

                            ```python
                            DocumentSplitter(split_by='sentence', split_length=10, max_tokens=512, max_chars=2000)
                            ```

                            means:

                            - Documents will contain whole sentences
                            - Documents will contain at most 10 sentences
                            - Documents might contain less than 10 sentences if the maximum number of tokens is
                                reached earlier.
                            - Documents will never contain more than 2000 chars. Documents with a content length
                                above that value will be split on the 2000th character.

                            Note that the number of tokens might still be above the maximum if a single sentence
                            contains more than 512 tokens. In this case an `ERROR` log is emitted, but the document
                            is generated with whatever amount of tokens the first sentence has.

                            If the number of units is irrelevant, `split_length` can be safely set at 0.

        :param add_page_number: Add the number of the page a paragraph occurs in to the Document's meta
                                field `"page"`. Page boundaries are determined by `"\f"' character which is added
                                in between pages by `PDFToTextConverter`, `TikaConverter`, `ParsrConverter` and
                                `AzureConverter`.

        :param progress_bar: Whether to show a progress bar.
        """
        split_documents = self.split(
            documents=documents,
            split_by=split_by,
            split_separators=split_separators,
            split_regex=split_regex,
            split_length=split_length,
            split_overlap=split_overlap,
            max_chars=max_chars,
            max_tokens=max_tokens,
            add_page_number=add_page_number,
            progress_bar=progress_bar,
        )
        return {"documents": split_documents}, "output_1"

    def run_batch(  # type: ignore
        self,
        documents: List[List[Document]],
        split_by: Optional[SplitBy] = None,
        split_regex: Optional[str] = None,
        split_length: Optional[int] = None,
        split_overlap: Optional[int] = None,
        split_max_chars: Optional[int] = None,
        add_page_number: Optional[bool] = None,
        progress_bar: Optional[bool] = True,
    ):
        """
        Splits documents into smaller, shorter documents.

        Can split on different units ('character', 'token', 'word', 'sentence', 'paragraph', 'page', or 'regex'),
        at different lengths, and include some overlap across the splits.

        It can also properly assign page numbers and re-assign headlines found in the metadata of the parent document
        to each split document.

        No char is lost in splitting, not even whitespace, and all headlines are preserved, However, text and headlines
        duplication may occur if `split_overlap>0`.

        :param documents: The documents to split.
        :param split_by: Unit for splitting the document. Can be 'character', 'token', 'word', 'sentence', 'paragraph', 'page', 'regex'.
        :param split_regex: If `split_by="regex"`, provide here a regex matching the separator. For example, if the document
                            should be split on "--my separator--", this field should be `split_regex="--my separator--"`.
        :param split_length: The maximum number of the above split unit (like word, sentence, page and so on) that are allowed in one document.
                                For instance, if `split_length=10` and `split_by="sentence"`, then each output document will contain 10 sentences.
        :param split_overlap: Units (for example words or sentences) overlap between two adjacent documents after a split.
                                For example, if `split_by="word" and split_length=5 and split_overlap=2`, then the splits would be like:
                                `[w1 w2 w3 w4 w5, w4 w5 w6 w7 w8, w7 w8 w10 w11 w12]`.
                                Set the value to 0 to ensure there is no overlap among the documents after splitting.
        :param max_chars: Absolute maximum number of chars allowed in a single document. Reaching this boundary
                            cuts the document, even mid-word, and logs a loud error.\n
                            It's recommended to set this value to approximately double the size you expect your documents
                            to be. For example, with `split_by='sentence'`, `split_length=2`, if the average sentence
                            length of our document is 100 chars, you should set `max_char=400` or `max_char=500`.\n
                            This is a safety parameter to avoid extremely long documents to end up in the document store.
                            Keep in mind that huge documents (tens of thousands of chars) will strongly impact the
                            performance of Reader nodes and can drastically slow down the indexing speed.
        :param max_tokens:  Maximum number of tokens that are allowed in a single split. If set to 0, it will be
                            ignored. If set to any value above 0, it requires `tokenizer_model` to be set to the
                            model of your Reader and will verify that, whatever your `split_length` value is set
                            to, the number of tokens included in the split documents will never be above the
                            `max_tokens` value. For example:

                            ```python
                            DocumentSplitter(split_by='sentence', split_length=10, max_tokens=512, max_chars=2000)
                            ```

                            means:

                            - Documents will contain whole sentences
                            - Documents will contain at most 10 sentences
                            - Documents might contain less than 10 sentences if the maximum number of tokens is
                                reached earlier.
                            - Documents will never contain more than 2000 chars. Documents with a content length
                                above that value will be split on the 2000th character.

                            Note that the number of tokens might still be above the maximum if a single sentence
                            contains more than 512 tokens. In this case an `ERROR` log is emitted, but the document
                            is generated with whatever amount of tokens the first sentence has.

                            If the number of units is irrelevant, `split_length` can be safely set at 0.

        :param add_page_number: Add the number of the page a paragraph occurs in to the Document's meta
                                field `"page"`. Page boundaries are determined by `"\f"' character which is added
                                in between pages by `PDFToTextConverter`, `TikaConverter`, `ParsrConverter` and
                                `AzureConverter`.

        :param progress_bar: Whether to show a progress bar.
        """
        documents = [
            self.run(
                documents=docs,
                split_by=split_by,
                split_regex=split_regex,
                split_length=split_length,
                split_overlap=split_overlap,
                max_chars=split_max_chars,
                add_page_number=add_page_number,
                progress_bar=False,
            )[0]["documents"]
            for docs in tqdm(
                documents,
                disable=not (progress_bar if progress_bar is not None else self.progress_bar),
                desc="Splitting",
                unit="docs",
            )
        ]
        return {"documents": documents}, "output_1"

    def split(
        self,
        documents: List[Document],
        split_by: Optional[SplitBy] = None,
        split_regex: Optional[str] = None,
        split_separators: Optional[List[str]] = None,
        split_length: Optional[int] = None,
        split_overlap: Optional[int] = None,
        max_chars: Optional[int] = None,
        max_tokens: Optional[int] = None,
        add_page_number: Optional[bool] = None,
        progress_bar: Optional[bool] = None,
    ) -> List[Document]:
        """
        Splits documents into smaller, shorter documents.

        Can split on different units ('character', 'token', 'word', 'sentence', 'paragraph', 'page', or 'regex'),
        at different lengths, and include some overlap across the splits.

        It can also properly assign page numbers and re-assign headlines found in the metadata of the parent document
        to each split document.

        No char is lost in splitting, not even whitespace, and all headlines are preserved, However, text and headlines
        duplication may occur if `split_overlap>0`.

        :param documents: The documents to split.
        :param split_by: Unit for splitting the document. Can be 'character', 'token', 'word', 'sentence', 'paragraph', 'page', 'regex'.
        :param split_regex: If `split_by="regex"`, provide here a regex matching the separator. For example, if the document
                            should be split on "--my separator--", this field should be `split_regex="--my separator--"`.
        :param split_length: The maximum number of the above split unit (like word, sentence, page and so on) that are allowed in one document.
                                For instance, if `split_length=10` and `split_by="sentence"`, then each output document will contain 10 sentences.
        :param split_overlap: Units (for example words or sentences) overlap between two adjacent documents after a split.
                                For example, if `split_by="word" and split_length=5 and split_overlap=2`, then the splits would be like:
                                `[w1 w2 w3 w4 w5, w4 w5 w6 w7 w8, w7 w8 w10 w11 w12]`.
                                Set the value to 0 to ensure there is no overlap among the documents after splitting.
        :param max_chars: Absolute maximum number of chars allowed in a single document. Reaching this boundary
                            cuts the document, even mid-word, and logs a loud error.\n
                            It's recommended to set this value to approximately double the size you expect your documents
                            to be. For example, with `split_by='sentence'`, `split_length=2`, if the average sentence
                            length of our document is 100 chars, you should set `max_char=400` or `max_char=500`.\n
                            This is a safety parameter to avoid extremely long documents to end up in the document store.
                            Keep in mind that huge documents (tens of thousands of chars) will strongly impact the
                            performance of Reader nodes and can drastically slow down the indexing speed.
        :param max_tokens:  Maximum number of tokens that are allowed in a single split. If set to 0, it will be
                            ignored. If set to any value above 0, it requires `tokenizer_model` to be set to the
                            model of your Reader and will verify that, whatever your `split_length` value is set
                            to, the number of tokens included in the split documents will never be above the
                            `max_tokens` value. For example:

                            ```python
                            DocumentSplitter(split_by='sentence', split_length=10, max_tokens=512, max_chars=2000)
                            ```

                            means:

                            - Documents will contain whole sentences
                            - Documents will contain at most 10 sentences
                            - Documents might contain less than 10 sentences if the maximum number of tokens is
                                reached earlier.
                            - Documents will never contain more than 2000 chars. Documents with a content length
                                above that value will be split on the 2000th character.

                            Note that the number of tokens might still be above the maximum if a single sentence
                            contains more than 512 tokens. In this case an `ERROR` log is emitted, but the document
                            is generated with whatever amount of tokens the first sentence has.

                            If the number of units is irrelevant, `split_length` can be safely set at 0.

        :param add_page_number: Add the number of the page a paragraph occurs in to the Document's meta
                                field `"page"`. Page boundaries are determined by `"\f"' character which is added
                                in between pages by `PDFToTextConverter`, `TikaConverter`, `ParsrConverter` and
                                `AzureConverter`.

        :param progress_bar: Whether to show a progress bar.
        """

        split_by = split_by if split_by is not None else self.split_by
        split_regex = split_regex if split_regex is not None else self.split_regex
        split_separators = split_separators if split_separators is not None else self.split_separators
        split_length = split_length if split_length is not None else self.split_length
        split_overlap = split_overlap if split_overlap is not None else self.split_overlap
        max_chars = max_chars if max_chars is not None else self.max_chars
        max_tokens = max_tokens if max_tokens is not None else self.max_tokens
        add_page_number = add_page_number if add_page_number is not None else self.add_page_number
        progress_bar if progress_bar is not None else self.progress_bar

        self._validate_split_params(
            split_by=split_by,
            split_regex=split_regex,
            split_separators=split_separators,
            split_length=split_length,
            split_overlap=split_overlap,
            max_chars=max_chars,
            max_tokens=max_tokens,
        )

        if any(document.content_type != "text" for document in documents):
            raise ValueError(
                "Some documents do not contain text. Make sure to pass only text documents to this node. "
                "You can use a RouteDocuments node to make sure only text documents are sent to the DocumentCleaner."
            )

        if split_by == "token" and not self.tokenizer:
            raise ValueError(
                "If you set split_by='token', you must give a value to 'tokenizer_model'. "
                "Use the same model you're using for your Reader, or `word` for a "
                "whitespace-based (very fast) tokenization."
            )

        # Get the function to use to split text
        splitter_function = self.get_splitter(
            split_by=split_by, split_regex=split_regex, split_separators=split_separators
        )

        final_documents = []
        for document in tqdm(documents, disable=not self.progress_bar, desc="Splitting", unit="docs"):

            if not document.content:
                logger.warning(
                    "An empty document was found: %s. It will be removed from the documents list.", repr(document)
                )
                continue

            # If we need to count the tokens, split by token
            tokens = None
            if max_tokens:
                if isinstance(self.tokenizer, (PreTrainedTokenizer, FeatureExtractor)):
                    tokens = split_by_transformers_tokenizer(text=document.content, tokenizer=self.tokenizer)
                else:
                    tokens = self.tokenizer(document.content)

            # Split the original doc into units (sentences, words, ...)
            split_content = splitter_function(text=document.content)

            # Merge them back according to the given split_length, split_overlap, max_tokens and/or max_chars values
            valid_contents = validate_unit_boundaries(
                contents=split_content, max_chars=max_chars, max_tokens=max_tokens, tokens=tokens
            )
            windows = make_merge_groups(
                contents=valid_contents,
                window_size=split_length,
                window_overlap=split_overlap,
                max_chars=max_chars,
                max_tokens=max_tokens,
            )

            # Assemble the document objects
            split_documents = [" ".join([valid_contents[doc_index][0] for doc_index in window]) for window in windows]
            split_metadata = fix_metadata(
                source_text=document.content, preprocessed_texts=split_documents, source_meta=document.meta
            )

            final_documents += [
                Document(content=split_doc, meta=split_meta, id_hash_keys=document.id_hash_keys)
                for split_doc, split_meta in zip(split_documents, split_metadata)
            ]

        return final_documents

    def get_splitter(self, split_by, split_regex, split_separators):
        """
        Returns the function that can be used for splitting the document.

        The function always takes only a string, text, as parameter and returns a list of strings.
        """
        if split_by == "regex":
            if not split_regex:
                raise ValueError("If 'split_by' is set to 'regex', you must give a value to 'split_regex'.")
            return partial(split_by_regex, pattern=split_regex)

        elif split_by == "separators":
            if not split_separators:
                raise ValueError("If 'split_by' is set to 'separators', you must give a value to 'split_separators'.")
            return partial(split_by_separators, separators=split_separators)

        elif split_by == "page":
            return lambda text: split_by_separator(text=text, separator="\f")

        elif split_by == "paragraph":
            return lambda text: split_by_separator(text=text, separator="\n\n")

        elif split_by == "sentence":
            try:
                nltk.data.find("tokenizers/punkt")
            except LookupError:
                nltk.download("punkt")

            if not self.sentence_tokenizer:
                self.sentence_tokenizer = load_sentence_tokenizer(
                    language=self.nltk_language, tokenizer_model_folder=self.nltk_folder
                )
            return lambda text: split_by_sentence_tokenizer(text=text, tokenizer=self.sentence_tokenizer)

        elif split_by == "word":
            return lambda text: split_by_separators(text=text, separators=[" ", "\n", "\f"])

        elif split_by == "token":
            if isinstance(self.tokenizer, (PreTrainedTokenizer, FeatureExtractor)):
                return lambda text: split_by_transformers_tokenizer(text=text, tokenizer=self.tokenizer)[0]
            else:
                return lambda text: self.tokenizer(text=text)

        raise ValueError(
            "split_by must be either 'character', 'word', 'sentence', 'paragraph', 'page', 'regex', 'separators'"
        )


def fix_metadata(source_text: str, preprocessed_texts: List[str], source_meta: Dict[str, Any]):
    return [source_meta] * len(preprocessed_texts)

    headlines_to_assign = document.meta.get("headlines") or []  # deepcopy(document.meta.get("headlines", [])) or []
    unit_documents = []
    pages = 1
    position_in_document = 0

    # Shortcut 1: no headlines, no page number
    if not headlines_to_assign and not add_page_number:
        meta = {key: value for key, value in document.meta.items() if key != "page"}

        for text in units[0]:
            unit_document = {"content": text, "meta": meta, "id_hash_keys": document.id_hash_keys}
            unit_documents.append(unit_document)

        return unit_documents, units[1]

    # Shortcut 2: no headlines
    elif not headlines_to_assign:
        for text in units[0]:
            unit_meta = {key: value for key, value in document.meta.items()}
            unit_meta["page"] = pages
            pages += text.count("\f")

            unit_document = {"content": text, "meta": unit_meta, "id_hash_keys": document.id_hash_keys}
            unit_documents.append(unit_document)

        return unit_documents, units[1]

    else:
        for text in units[0]:

            # Find the relevant headlines for this unit
            unit_headlines = []
            other_headlines = []
            for headline in headlines_to_assign:
                if position_in_document <= headline["start_idx"] < position_in_document + len(text):
                    headline["start_idx"] -= position_in_document
                    unit_headlines.append(headline)
                else:
                    other_headlines.append(headline)

            position_in_document += len(text)
            headlines_to_assign = other_headlines

            # Clone the meta from the parent document
            unit_meta = {key: value for key, value in document.meta.items()}

            # If the parent had headlines, but this unit happens not to have them, we assign an empty list
            # If the parent never had a headlines field, we don't create it here.
            if "headlines" in unit_meta and unit_meta["headlines"]:
                unit_meta["headlines"] = unit_headlines

            # Assign page number if required
            if "page" in unit_meta:
                del unit_meta["page"]
            if add_page_number:
                unit_meta["page"] = pages
                pages += text.count("\f")

            # Create the document
            unit_document = {"content": text, "meta": unit_meta, "id_hash_keys": document.id_hash_keys}
            unit_documents.append(unit_document)

    return unit_documents, units[1]