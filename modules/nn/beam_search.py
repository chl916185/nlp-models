from typing import List, Callable, Tuple, Dict

import torch

from allennlp.common.checks import ConfigurationError


StateType = Dict[str, torch.Tensor]  # pylint: disable=invalid-name
StepFunctionType = Callable[[torch.Tensor, StateType], Tuple[torch.Tensor, StateType]]  # pylint: disable=invalid-name


class BeamSearch:
    """
    Implements the beam search algorithm for decoding the most likely sequences.

    Parameters
    ----------
    end_index : ``int``
        The index of the "stop" or "end" token in the target vocabulary.
    max_steps : ``int``, optional (default = 50)
        The maximum number of decoding steps to take, i.e. the maximum length
        of the predicted sequences.
    beam_size : ``int``, optional (default = 10)
        The width of the beam used.
    per_node_beam_size : ``int``, optional (default = beam_size)
        The maximum number of candidates to consider per node, at each step in the search.
        If not given, this just defaults to ``beam_size``. Setting this parameter
        to a number smaller than ``beam_size`` may give better results, as it can introduce
        more diversity into the search. See `Beam Search Strategies for Neural Machine Translation.
        Freitag and Al-Onaizan, 2017 <http://arxiv.org/abs/1702.01806>`_.
    """

    def __init__(self,
                 end_index: int,
                 max_steps: int = 50,
                 beam_size: int = 10,
                 per_node_beam_size: int = None) -> None:
        self._end_index = end_index
        self.max_steps = max_steps
        self.beam_size = beam_size
        self.per_node_beam_size = per_node_beam_size or beam_size

    def search(self,
               start_predictions: torch.Tensor,
               start_state: StateType,
               step: StepFunctionType,
               first_step: StepFunctionType = None) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Given a starting state and a step function, apply beam search to find the
        most likely target sequences.

        Parameters
        ----------
        start_predictions : ``torch.Tensor``
            A tensor containing the initial predictions with shape ``(batch_size,)``.
            Usually the initial predictions are just the index of the "start" token
            in the target vocabulary.
        start_state : ``StateType``
            The initial state passed to the ``first_step`` function. Each value of the state dict
            should be a tensor of shape ``(batch_size, *)``, where ``*`` means any other
            number of dimensions.
        step : ``StepFunctionType``
            A function that is responsible for computing the next most likely tokens,
            given the current state and the predictions from the last time step.
            The function should accept two arguments. The first being a tensor
            of shape ``(group_size,)``, representing the index of the predicted
            tokens from the last time step, and the second being the current state.
            The ``group_size`` will be ``batch_size * beam_size``, except in the initial
            step, for which it will just be ``batch_size``.
            The function is expected to return a tuple, where the first element
            is a tensor of shape ``(group_size, target_vocab_size)`` containing
            the log probabilities of the tokens for the next step, and the second
            element is the updated state. The tensor in the state should have shape
            ``(group_size, *)``, where ``*`` means any other number of dimensions.
        first_step : ``StepFunctionType``, optional
            If the first step of decoding should be handled differently, then you can
            set this function which will only be used during the first step. If not set,
            ``step`` will be used for the first step as well. This function should have the
            same signature as ``step``.

        Returns
        -------
        Tuple[torch.Tensor, torch.Tensor]
            Tuple of ``(predictions, log_probabilities)``, where ``predictions``
            has shape ``(batch_size, beam_size, max_steps)`` and ``log_probabilities``
            has shape ``(batch_size, beam_size)``.
        """
        batch_size = start_predictions.size()[0]

        first_step = first_step or step

        # List of (batch_size, beam_size) tensors. One for each time step. Does not
        # include the start symbols, which are implicit.
        predictions: List[torch.Tensor] = []

        # List of (batch_size, beam_size) tensors. One for each time step. None for
        # the first.  Stores the index n for the parent prediction, i.e.
        # predictions[t-1][i][n], that it came from.
        backpointers: List[torch.Tensor] = []

        # Calculate the first timestep. This is done outside the main loop
        # because we are going from a single decoder input (the output from the
        # encoder) to the top `beam_size` decoder outputs. On the other hand,
        # within the main loop we are going from the `beam_size` elements of the
        # beam to `beam_size`^2 candidates from which we will select the top
        # `beam_size` elements for the next iteration.
        start_class_log_probabilities, state = first_step(start_predictions, start_state)
        # shape: (batch_size, num_classes)

        num_classes = start_class_log_probabilities.size()[1]

        # Make sure `per_node_beam_size` is not larger than `num_classes`.
        if self.per_node_beam_size > num_classes:
            raise ConfigurationError(f"Target vocab size ({num_classes:d}) too small "
                                     f"relative to per_node_beam_size ({self.per_node_beam_size:d}).\n"
                                     f"Please decrease beam_size or per_node_beam_size.")

        start_top_log_probabilities, start_predicted_classes = \
                start_class_log_probabilities.topk(self.beam_size)
        # shape: (batch_size, beam_size), (batch_size, beam_size)

        # The log probabilities for the last time step.
        last_log_probabilities = start_top_log_probabilities
        # shape: (batch_size, beam_size)

        predictions.append(start_predicted_classes)
        # shape: [(batch_size, beam_size)]

        # Log probability tensor that mandates that the end token is selected.
        log_probs_after_end = start_class_log_probabilities.new_full(
                (batch_size * self.beam_size, num_classes),
                float("-inf")
        )
        log_probs_after_end[:, self._end_index] = 0.
        # shape: (batch_size * beam_size, num_classes)

        # Set the same state for each element in the beam.
        for key, state_tensor in state.items():
            _, *last_dims = state_tensor.size()
            state[key] = state_tensor.\
                    unsqueeze(1).\
                    expand(batch_size, self.beam_size, *last_dims).\
                    reshape(batch_size * self.beam_size, *last_dims)
            # shape: (batch_size * beam_size, *)

        for timestep in range(self.max_steps - 1):
            last_predictions = predictions[-1].reshape(batch_size * self.beam_size)
            # shape: (batch_size * beam_size,)

            # If every predicted token from the last step is `self._end_index`,
            # then we can stop early.
            if (last_predictions == self._end_index).all():
                break

            # Take a step. This get the predicted log probs of the next classes
            # and updates the state.
            class_log_probabilities, state = step(last_predictions, state)
            # shape: (batch_size * beam_size, num_classes)

            last_predictions_expanded = last_predictions.unsqueeze(-1).expand(
                    batch_size * self.beam_size,
                    num_classes
            )
            # shape: (batch_size * beam_size, num_classes)

            # Here we are finding any beams where we predicted the end token in
            # the previous timestep and replacing the distribution with a
            # one-hot distribution, forcing the beam to predict the end token
            # this timestep as well.
            cleaned_log_probabilities = torch.where(
                    last_predictions_expanded == self._end_index,
                    log_probs_after_end,
                    class_log_probabilities
            )
            # shape: (batch_size * beam_size, num_classes)

            top_log_probabilities, predicted_classes = \
                cleaned_log_probabilities.topk(self.per_node_beam_size)
            # shape (both): (batch_size * beam_size, per_node_beam_size)

            # Here we expand the last log probabilities to (batch_size * beam_size, per_node_beam_size)
            # so that we can add them to the current log probs for this timestep.
            # This lets us maintain the log probability of each element on the beam.
            expanded_last_log_probabilities = last_log_probabilities.\
                    unsqueeze(2).\
                    expand(batch_size, self.beam_size, self.per_node_beam_size).\
                    reshape(batch_size * self.beam_size, self.per_node_beam_size)
            # shape: (batch_size * beam_size, per_node_beam_size)

            summed_top_log_probabilities = top_log_probabilities + expanded_last_log_probabilities
            # shape: (batch_size * beam_size, per_node_beam_size)

            reshaped_summed = summed_top_log_probabilities.\
                    reshape(batch_size, self.beam_size * self.per_node_beam_size)
            # shape: (batch_size, beam_size * per_node_beam_size)

            reshaped_predicted_classes = predicted_classes.\
                    reshape(batch_size, self.beam_size * self.per_node_beam_size)
            # shape: (batch_size, beam_size * per_node_beam_size)

            # Keep only the top `beam_size` beam indices.
            restricted_beam_log_probs, restricted_beam_indices = reshaped_summed.topk(self.beam_size)
            # shape: (batch_size, beam_size), (batch_size, beam_size)

            # Use the beam indices to extract the corresponding classes.
            restricted_predicted_classes = reshaped_predicted_classes.gather(1, restricted_beam_indices)
            # shape: (batch_size, beam_size)

            predictions.append(restricted_predicted_classes)

            last_log_probabilities = restricted_beam_log_probs
            # shape: (batch_size, beam_size)

            # The beam indices come from a `beam_size * per_node_beam_size` dimension where the
            # indices with a common ancestor are grouped together. Hence
            # dividing by per_node_beam_size gives the ancestor. (Note that this is integer
            # division as the tensor is a LongTensor.)
            backpointer = restricted_beam_indices / self.per_node_beam_size
            # shape: (batch_size, beam_size)

            backpointers.append(backpointer)

            # Keep only the pieces of the state tensors corresponding to the
            # ancestors created this iteration.
            for key, state_tensor in state.items():
                _, *last_dims = state_tensor.size()
                expanded_backpointer = backpointer.\
                        view(batch_size, self.beam_size, *([1] * len(last_dims))).\
                        expand(batch_size, self.beam_size, *last_dims)
                # shape: (batch_size, beam_size, *)

                state[key] = state_tensor.\
                        reshape(batch_size, self.beam_size, *last_dims).\
                        gather(1, expanded_backpointer).\
                        reshape(batch_size * self.beam_size, *last_dims)
                # shape: (batch_size * beam_size, *)

        # Reconstruct the sequences.
        reconstructed_predictions = [predictions[-1].unsqueeze(2)]
        # shape: [(batch_size, beam_size, 1)]

        cur_backpointers = backpointers[-1]
        # shape: (batch_size, beam_size)

        for timestep in range(len(predictions) - 2, 0, -1):
            cur_preds = predictions[timestep].gather(1, cur_backpointers).unsqueeze(2)
            # shape: (batch_size, beam_size, 1)

            reconstructed_predictions.append(cur_preds)

            cur_backpointers = backpointers[timestep - 1].gather(1, cur_backpointers)
            # shape: (batch_size, beam_size)

        final_preds = predictions[0].gather(1, cur_backpointers).unsqueeze(2)
        # shape: (batch_size, beam_size, 1)

        reconstructed_predictions.append(final_preds)

        all_predictions = torch.cat(list(reversed(reconstructed_predictions)), 2)
        # shape: (batch_size, beam_size, max_steps)

        return all_predictions, last_log_probabilities
